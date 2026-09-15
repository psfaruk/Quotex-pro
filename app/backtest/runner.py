"""Backtest engine: replays collected closed candles through the same
confluence logic used live (tick-level micro factors approximated from OHLC+
stored tick stats), then reports honest win rates.

No lookahead: signal for candle N+1 uses only candles <= N."""
import asyncio
import time
from typing import Dict, List, Optional

from ..models import Candle
from ..storage import db
from ..engine.analysis import compute_factors


def _candle_from_row(row: Dict) -> Candle:
    c = Candle(minute=row["minute"], pair=row["pair"])
    c.open, c.high, c.low, c.close = row["open"], row["high"], row["low"], row["close"]
    c.ticks = row.get("ticks", 0) or 0
    c.up_ticks = row.get("up", 0) or 0
    c.down_ticks = row.get("down", 0) or 0
    c.last10_up = row.get("l10u", 0) or 0
    c.last10_down = row.get("l10d", 0) or 0
    c.closed = True
    return c


async def run_backtest(core, pair: Optional[str], limit: int,
                       threshold: Optional[float] = None,
                       min_confidence: Optional[int] = None) -> Dict:
    threshold = threshold if threshold is not None else core.settings.threshold
    min_conf = min_confidence if min_confidence is not None else core.settings.min_confidence

    pairs = [pair] if pair else core.settings.pairs
    report = {"pairs": {}, "threshold": threshold, "min_confidence": min_conf,
              "generated_at": time.time()}

    agg = {"signals": 0, "win": 0, "loss": 0, "tie": 0,
           "call": {"win": 0, "loss": 0}, "put": {"win": 0, "loss": 0},
           "strong": {"win": 0, "loss": 0}, "medium": {"win": 0, "loss": 0},
           "weak": {"win": 0, "loss": 0}}

    for p in pairs:
        rows = await db.load_candles(p, limit)
        # dedupe + sort by minute
        seen = {}
        for r in rows:
            seen[r["minute"]] = r
        rows = [seen[m] for m in sorted(seen)]
        candles = [_candle_from_row(r) for r in rows]
        if len(candles) < 25:
            report["pairs"][p] = {"error": "not enough candles", "candles": len(candles)}
            continue

        pw = {"signals": 0, "win": 0, "loss": 0, "tie": 0,
              "call": {"win": 0, "loss": 0}, "put": {"win": 0, "loss": 0},
              "strong": {"win": 0, "loss": 0}, "medium": {"win": 0, "loss": 0},
              "weak": {"win": 0, "loss": 0}}

        # replay: for each i (>= warmup), candle[i] just closed -> predict i+1
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
            pw["signals"] += 1
            pw[res] += 1
            if res in ("win", "loss"):
                pw[direction.lower()][res] += 1
                pw[tier][res] += 1
            agg["signals"] += 1
            agg[res] += 1
            if res in ("win", "loss"):
                agg[direction.lower()][res] += 1
                agg[tier][res] += 1

        def wr(w, l):
            return round(100.0 * w / (w + l), 1) if (w + l) else 0.0

        pw["winrate"] = wr(pw["win"], pw["loss"])
        pw["candles_tested"] = len(candles)
        pw["call_winrate"] = wr(pw["call"]["win"], pw["call"]["loss"])
        pw["put_winrate"] = wr(pw["put"]["win"], pw["put"]["loss"])
        for t in ("strong", "medium", "weak"):
            pw[t] = {**pw[t], "winrate": wr(pw[t]["win"], pw[t]["loss"])}
        report["pairs"][p] = pw

    def wr(w, l):
        return round(100.0 * w / (w + l), 1) if (w + l) else 0.0

    report["total"] = {
        "signals": agg["signals"], "win": agg["win"], "loss": agg["loss"], "tie": agg["tie"],
        "winrate": wr(agg["win"], agg["loss"]),
        "call_winrate": wr(agg["call"]["win"], agg["call"]["loss"]),
        "put_winrate": wr(agg["put"]["win"], agg["put"]["loss"]),
        "strong": {**agg["strong"], "winrate": wr(agg["strong"]["win"], agg["strong"]["loss"])},
        "medium": {**agg["medium"], "winrate": wr(agg["medium"]["win"], agg["medium"]["loss"])},
        "weak": {**agg["weak"], "winrate": wr(agg["weak"]["win"], agg["weak"]["loss"])},
    }
    return report
