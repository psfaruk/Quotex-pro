"""DEEP BACKTEST — candle parity + signal performance on REAL Quotex data.

Uses the live-captured history/list/v2 payloads (3 pairs, ~500s of ticks +
198 server-computed M1 candles each) to prove the app's chart matches the
Quotex terminal candle-for-candle, and to replay the signal engine on REAL
broker candles.

  TEST 1  parity replay   — feed real ticks through the FIXED CandleEngine,
                             push the real server payload exactly like the
                             live app does, then verify: every COMPLETE
                             server minute's engine candle == server OHLC
                             EXACTLY (what the Quotex terminal draws).
  TEST 2  stale-tail guard — the trailing stale rows (mid-minute snapshots,
                             e.g. ticks=8 of ~140) must NOT corrupt the
                             locally live-collected candles: engine candle
                             must equal the FULL tick aggregation (truth).
                             Also quantifies what the OLD code would have
                             broken (stale overwrite vs truth).
  TEST 3  late-tick guard — an out-of-order tick across a minute boundary
                             must land in its own candle, never pollute the
                             running candle.
  TEST 4  signal backtest — replay all 198 REAL server candles per pair
                             through the app's confluence engine (identical
                             code path to the live /api/backtest) and settle
                             predictions against the next REAL candle.
"""
import json
import sys
from collections import defaultdict

sys.path.insert(0, "/home/z/my-project/qx-signal-pro")
from app.engine.candles import CandleEngine          # noqa: E402
from app.engine.analysis import compute_factors      # noqa: E402

CAPTURES = [
    "/home/z/my-project/scripts/probe2_history_list_v2_1.bin",
    "/home/z/my-project/scripts/probe2_history_list_v2_2.bin",
    "/home/z/my-project/scripts/probe2_history_list_v2_3.bin",
]


def load(path):
    raw = open(path, "rb").read()
    if raw[:1] == b"\x04":
        raw = raw[1:]
    return json.loads(raw.decode("utf-8"))


def aggregate_truth(hist):
    """Full tick aggregation per minute = the ground truth candle that the
    Quotex server itself computes once a minute completes."""
    agg = {}
    for ts, price, _flag in hist:
        m = int(ts // 60) * 60
        a = agg.setdefault(m, {"o": None, "h": -1e18, "l": 1e18, "c": None, "n": 0})
        if a["o"] is None:
            a["o"] = price
        a["h"] = max(a["h"], price)
        a["l"] = min(a["l"], price)
        a["c"] = price
        a["n"] += 1
    return agg


def run_parity(data):
    pair = data["asset"]
    hist = data["history"]
    rows = data["candles"]
    now_hint = float(hist[-1][0])              # server-clock evidence
    truth = aggregate_truth(hist)
    srv = {int(r[0]): r for r in rows}

    eng = CandleEngine(pair)
    # replay ticks exactly like the live stream delivers them
    for ts, price, _flag in hist:
        eng.add_tick(float(price), float(ts))
    # push the server payload exactly like core._on_history does
    res = eng.seed_server_candles(rows, now_hint=now_hint)

    report = {"pair": pair, "complete_ok": 0, "complete_n": 0,
              "stale_ok": 0, "stale_n": 0, "old_bug_mismatch": 0,
              "corrections": len(res.get("corrections") or [])}

    for m in sorted(truth):
        t = truth[m]
        row = srv.get(m)
        eng_c = None
        for c in eng.candles:
            if c.minute == m:
                eng_c = c
                break
        if eng_c is None:
            continue
        if row is not None:
            complete = float(row[6]) >= m + 50
            if complete:
                report["complete_n"] += 1
                ok = (eng_c.open == row[1] and eng_c.close == row[2]
                      and eng_c.high == row[3] and eng_c.low == row[4])
                report["complete_ok"] += int(ok)
            else:
                # stale row: engine must keep the LIVE truth, not the snapshot
                report["stale_n"] += 1
                ok = (eng_c.open == t["o"] and eng_c.close == t["c"]
                      and eng_c.high == t["h"] and eng_c.low == t["l"])
                report["stale_ok"] += int(ok)
                # what the OLD code would have shown (stale overwrite):
                if (row[1], row[2], row[3], row[4]) != (t["o"], t["c"], t["h"], t["l"]):
                    report["old_bug_mismatch"] += 1
        else:
            # minute absent from server rows (fresh tail) -> local truth kept
            report["stale_n"] += 1
            ok = (eng_c.open == t["o"] and eng_c.close == t["c"]
                  and eng_c.high == t["h"] and eng_c.low == t["l"])
            report["stale_ok"] += int(ok)

    # running candle parity: engine close == latest real tick price
    report["running_close_ok"] = eng.running.close == float(hist[-1][1])
    return report, eng


def run_late_tick_guard():
    eng = CandleEngine("TEST")
    base = 1789470000                       # a minute boundary
    eng.add_tick(1.1000, base + 5)
    eng.add_tick(1.1005, base + 15)
    eng.add_tick(1.1010, base + 25)
    # minute rolls over -> first close, new running candle
    eng.add_tick(1.1008, base + 60 + 2)
    # LATE tick from the PREVIOUS minute arrives after the new one started
    eng.add_tick(1.1030, base + 58)
    eng.add_tick(1.0990, base + 45)          # even older, must not extend
    run = eng.running
    prev = eng.candles[-1]
    ok = (
        run.minute == base + 60
        and run.open == 1.1008 and run.close == 1.1008 and run.ticks == 1
        # late 1.1030 @+58 extends the closed candle's high (server would too)
        and prev.high == 1.1030 and prev.close == 1.1030
        # the even older 1.0990 @+45 must NOT change anything (ts <= last_ts)
        and prev.low == 1.1000
    )
    return ok, eng


def run_signal_backtest(data, threshold=22.0, min_conf=63):
    """Replay REAL server candles through the app's exact signal logic."""
    pair = data["asset"]
    rows = data["candles"]
    # oldest-first, only COMPLETE rows (what the Quotex terminal actually drew)
    candles = []
    for r in sorted(rows, key=lambda r: r[0]):
        minute, o, c, h, l, n, last_ts = (int(r[0]), float(r[1]), float(r[2]),
                                          float(r[3]), float(r[4]), int(r[5]),
                                          float(r[6]))
        if last_ts < minute + 50:
            continue                          # skip stale snapshots
        from app.models import Candle
        cd = Candle(minute=minute, pair=pair, open=o, high=h, low=l, close=c)
        cd.ticks = n
        cd.closed = True
        # approximate tick micro stats from OHLC (same as DB replay path)
        rng = max(h - l, 1e-12)
        if c >= o:
            cd.up_ticks = max(n // 2, 1)
            cd.down_ticks = max(n - cd.up_ticks, 0)
        else:
            cd.down_ticks = max(n // 2, 1)
            cd.up_ticks = max(n - cd.down_ticks, 0)
        cp = (c - l) / rng
        cd.last10_up = int(max(0.0, cp - 0.5) * 2 * max(n // 6, 1))
        cd.last10_down = int(max(0.0, 0.5 - cp) * 2 * max(n // 6, 1))
        candles.append(cd)

    pw = {"signals": 0, "win": 0, "loss": 0, "tie": 0,
          "call": {"win": 0, "loss": 0}, "put": {"win": 0, "loss": 0},
          "strong": {"win": 0, "loss": 0}, "medium": {"win": 0, "loss": 0},
          "weak": {"win": 0, "loss": 0}, "candles": len(candles)}
    warmup = 25
    for i in range(warmup, len(candles) - 1):
        hist = candles[:i + 1]
        closed = candles[i]
        nxt = candles[i + 1]
        score, _f = compute_factors(closed, hist, live=True)
        conf = int(round(50 + min(abs(score), 120.0) * 0.375))
        if abs(score) < threshold or conf < min_conf:
            continue
        direction = "CALL" if score > 0 else "PUT"
        diff = nxt.close - closed.close
        res = "tie" if abs(diff) < 1e-12 else (
            "win" if (diff > 0 and direction == "CALL") or
                     (diff < 0 and direction == "PUT") else "loss")
        tier = "strong" if conf >= 78 else ("medium" if conf >= 63 else "weak")
        pw["signals"] += 1
        pw[res] += 1
        if res in ("win", "loss"):
            pw[direction.lower()][res] += 1
            pw[tier][res] += 1

    def wr(w, l):
        return round(100.0 * w / (w + l), 1) if (w + l) else 0.0

    pw["winrate"] = wr(pw["win"], pw["loss"])
    pw["call_winrate"] = wr(pw["call"]["win"], pw["call"]["loss"])
    pw["put_winrate"] = wr(pw["put"]["win"], pw["put"]["loss"])
    for t in ("strong", "medium", "weak"):
        pw[t] = {**pw[t], "winrate": wr(pw[t]["win"], pw[t]["loss"])}
    return pw


def main():
    print("=" * 78)
    print("DEEP BACKTEST — REAL Quotex captured data (3 pairs)")
    print("=" * 78)

    all_reports = []
    agg = defaultdict(int)
    signal_total = {"signals": 0, "win": 0, "loss": 0, "tie": 0,
                    "strong": {"win": 0, "loss": 0},
                    "medium": {"win": 0, "loss": 0},
                    "weak": {"win": 0, "loss": 0}}
    for path in CAPTURES:
        data = load(path)
        rep, eng = run_parity(data)
        all_reports.append(rep)
        for k in ("complete_ok", "complete_n", "stale_ok", "stale_n",
                  "old_bug_mismatch", "corrections"):
            agg[k] += rep[k]
        print(f"\n[{rep['pair']}] parity replay")
        print(f"  COMPLETE server minutes : {rep['complete_ok']}/{rep['complete_n']}"
              f" engine OHLC == server OHLC (Quotex terminal)")
        print(f"  STALE/tail minutes kept : {rep['stale_ok']}/{rep['stale_n']}"
              f" live-truth preserved (old code would corrupt "
              f"{rep['old_bug_mismatch']})")
        print(f"  running candle close == latest real tick: {rep['running_close_ok']}")
        print(f"  corrections emitted    : {rep['corrections']}")

        pw = run_signal_backtest(data)
        print(f"  signal backtest on {pw['candles']} REAL candles: "
              f"{pw['signals']} signals, winrate {pw['winrate']}% "
              f"(CALL {pw['call_winrate']}% / PUT {pw['put_winrate']}%)")
        signal_total["signals"] += pw["signals"]
        signal_total["win"] += pw["win"]
        signal_total["loss"] += pw["loss"]
        signal_total["tie"] += pw["tie"]
        for t in ("strong", "medium", "weak"):
            for k in ("win", "loss"):
                signal_total[t][k] += pw[t][k]

    print("\n" + "=" * 78)
    print("PARITY SUMMARY (candle-for-candle match with Quotex terminal)")
    print("=" * 78)
    print(f"  COMPLETE minutes exact OHLC match : {agg['complete_ok']}/{agg['complete_n']}"
          f"  ({100*agg['complete_ok']/max(agg['complete_n'],1):.1f}%)")
    print(f"  LIVE-edge minutes truth preserved : {agg['stale_ok']}/{agg['stale_n']}"
          f"  ({100*agg['stale_ok']/max(agg['stale_n'],1):.1f}%)")
    print(f"  minutes the OLD code would have corrupted : {agg['old_bug_mismatch']}")

    ok, eng = run_late_tick_guard()
    print(f"  late-tick boundary guard          : {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("    running:", eng.running.to_dict() if eng.running else None)
        print("    closed :", eng.candles[-1].to_dict() if eng.candles else None)

    print("\n" + "=" * 78)
    print("SIGNAL BACKTEST on REAL Quotex candles (same engine as live app)")
    print("=" * 78)
    tot_w = signal_total["win"]
    tot_l = signal_total["loss"]
    tot_s = signal_total["signals"]
    tot_t = signal_total["tie"]
    for t in ("strong", "medium", "weak"):
        w, l = signal_total[t]["win"], signal_total[t]["loss"]
        wr = round(100.0 * w / (w + l), 1) if (w + l) else 0.0
        print(f"  {t:7s}: {w}W / {l}L  ->  {wr}%")
    wr = round(100.0 * tot_w / (tot_w + tot_l), 1) if (tot_w + tot_l) else 0.0
    print(f"  TOTAL  : {tot_s} signals, {tot_w}W/{tot_l}L/{tot_t}T -> winrate {wr}%")

    verdict = (agg["complete_ok"] == agg["complete_n"]
               and agg["stale_ok"] == agg["stale_n"] and ok)
    print("\nVERDICT:", "ALL PARITY TESTS PASS — chart matches Quotex "
          "candle-for-candle" if verdict else "PARITY FAILURE — see above")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
