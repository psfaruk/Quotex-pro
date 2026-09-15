#!/usr/bin/env python3.13
"""
Offline backtest from raw collected tick data (collector JSONL + boot files).

Builds M1 candles (with micro-structure stats) from raw ticks, then replays
the SAME confluence engine used live. Reports honest win rates.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/z/my-project/qx-signal-pro")

from app.models import Candle
from app.engine.analysis import compute_factors

COLLECTED = "/home/z/my-project/scripts/collected"


def load_ticks(pair: str):
    """Merge boot history + live jsonl, dedup, sort."""
    ticks = {}
    boot = f"{COLLECTED}/{pair}_boot.json"
    if os.path.exists(boot):
        for ts, price, flag in json.load(open(boot)):
            ticks[round(float(ts), 3)] = (float(price), int(flag))
    jsonl = f"{COLLECTED}/{pair}.jsonl"
    if os.path.exists(jsonl):
        for line in open(jsonl):
            try:
                ts, price, flag = json.loads(line)
                ticks[round(float(ts), 3)] = (float(price), int(flag))
            except Exception:
                pass
    return sorted(ticks.items())


def build_candles(pair: str, ticks):
    candles = []
    cur = None
    prev_price = None
    for ts, (price, _flag) in ticks:
        minute = int(ts // 60) * 60
        sec = ts - minute
        if cur is None or minute > cur.minute:
            if cur is not None:
                candles.append(cur)
            cur = Candle(minute=minute, pair=pair)
        if cur.ticks == 0:
            cur.open = cur.close = cur.high = cur.low = price
        else:
            if price > cur.high:
                cur.high = price
            elif price < cur.low:
                cur.low = price
            if prev_price is not None:
                if price > prev_price:
                    cur.up_ticks += 1
                    if sec >= 50:
                        cur.last10_up += 1
                elif price < prev_price:
                    cur.down_ticks += 1
                    if sec >= 50:
                        cur.last10_down += 1
        cur.close = price
        cur.ticks += 1
        prev_price = price
    if cur is not None:
        candles.append(cur)
    return candles


def backtest(pair: str, candles, threshold=35.0, min_conf=55):
    stats = {"signals": 0, "win": 0, "loss": 0, "tie": 0,
             "call": {"win": 0, "loss": 0}, "put": {"win": 0, "loss": 0},
             "strong": {"win": 0, "loss": 0}, "medium": {"win": 0, "loss": 0},
             "weak": {"win": 0, "loss": 0}}
    warmup = 25
    for i in range(warmup, len(candles) - 1):
        hist = candles[:i + 1]
        closed = candles[i]
        nxt = candles[i + 1]
        score, factors = compute_factors(closed, hist, live=True)
        conf = int(round(50 + min(abs(score), 120.0) * 0.375))
        if abs(score) < threshold or conf < min_conf:
            continue
        direction = "CALL" if score > 0 else "PUT"
        diff = nxt.close - closed.close
        if abs(diff) < 1e-12:
            res = "tie"
        elif (diff > 0 and direction == "CALL") or (diff < 0 and direction == "PUT"):
            res = "win"
        else:
            res = "loss"
        tier = "strong" if conf >= 78 else ("medium" if conf >= 63 else "weak")
        stats["signals"] += 1
        stats[res] += 1
        if res in ("win", "loss"):
            stats[direction.lower()][res] += 1
            stats[tier][res] += 1
    return stats


def wr(w, l):
    return f"{100.0*w/(w+l):.1f}%" if (w + l) else "n/a"


def load_server_candles(pair: str):
    """Closed candles persisted by the running app (extra source/dedupe)."""
    path = "/home/z/my-project/qx-signal-pro/data/qx.db"
    rows = []
    if not os.path.exists(path):
        return rows
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("SELECT * FROM candles WHERE pair=? ORDER BY minute", (pair,)):
            c = Candle(minute=r["minute"], pair=pair)
            c.open, c.high, c.low, c.close = r["open"], r["high"], r["low"], r["close"]
            c.ticks = r["ticks"]; c.up_ticks = r["up"]; c.down_ticks = r["down"]
            c.last10_up = r["l10u"]; c.last10_down = r["l10d"]
            c.closed = True
            rows.append(c)
    finally:
        con.close()
    return rows


def merge_candles(a, b):
    """Merge two candle lists by minute, preferring the one with more ticks."""
    by_min = {}
    for c in a + b:
        ex = by_min.get(c.minute)
        if ex is None or c.ticks > ex.ticks:
            by_min[c.minute] = c
    return [by_min[m] for m in sorted(by_min)]


def main():
    print("=" * 78)
    print("OFFLINE BACKTEST — raw tick data -> M1 candles -> live confluence engine")
    print("=" * 78)
    agg = {"signals": 0, "win": 0, "loss": 0, "tie": 0,
           "call": {"win": 0, "loss": 0}, "put": {"win": 0, "loss": 0},
           "strong": {"win": 0, "loss": 0}, "medium": {"win": 0, "loss": 0},
           "weak": {"win": 0, "loss": 0}}

    pairs = sorted(set(f.replace("_boot.json", "").replace(".jsonl", "")
                       for f in os.listdir(COLLECTED)))
    for pair in pairs:
        ticks = load_ticks(pair)
        if len(ticks) < 500:
            print(f"{pair}: only {len(ticks)} ticks — skipped")
            continue
        candles = build_candles(pair, ticks)
        candles = merge_candles(candles, load_server_candles(pair))
        span_min = (ticks[-1][0] - ticks[0][0]) / 60
        s = backtest(pair, candles)
        print(f"\n{pair}  ({len(ticks)} ticks, {span_min:.0f} min, {len(candles)} candles)")
        print(f"  signals={s['signals']}  WIN {s['win']} / LOSS {s['loss']} / TIE {s['tie']}  "
              f"winrate={wr(s['win'], s['loss'])}")
        print(f"  CALL {wr(s['call']['win'], s['call']['loss'])} "
              f"({s['call']['win']}W/{s['call']['loss']}L)   "
              f"PUT {wr(s['put']['win'], s['put']['loss'])} "
              f"({s['put']['win']}W/{s['put']['loss']}L)")
        print(f"  strong {wr(s['strong']['win'], s['strong']['loss'])}  "
              f"medium {wr(s['medium']['win'], s['medium']['loss'])}  "
              f"weak {wr(s['weak']['win'], s['weak']['loss'])}")
        for k in ("signals", "win", "loss", "tie"):
            agg[k] += s[k]
        for k in ("call", "put", "strong", "medium", "weak"):
            for r in ("win", "loss"):
                agg[k][r] += s[k][r]

    print("\n" + "=" * 78)
    print(f"TOTAL: signals={agg['signals']}  winrate={wr(agg['win'], agg['loss'])}  "
          f"(W{agg['win']}/L{agg['loss']}/T{agg['tie']})")
    print(f"  CALL {wr(agg['call']['win'], agg['call']['loss'])}   "
          f"PUT {wr(agg['put']['win'], agg['put']['loss'])}")
    print(f"  strong {wr(agg['strong']['win'], agg['strong']['loss'])}  "
          f"medium {wr(agg['medium']['win'], agg['medium']['loss'])}  "
          f"weak {wr(agg['weak']['win'], agg['weak']['loss'])}")
    print("=" * 78)


if __name__ == "__main__":
    main()
