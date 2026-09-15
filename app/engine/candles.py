"""Candle engine: tick -> M1 candle aggregation with micro-structure stats."""
import time
from typing import Callable, Dict, List, Optional

from ..models import Candle


class CandleEngine:
    """One instance per pair. Feeds ticks, emits running updates and closes."""

    MAX_HISTORY = 400

    def __init__(self, pair: str,
                 on_close: Optional[Callable] = None,
                 on_update: Optional[Callable] = None):
        self.pair = pair
        self.candles: List[Candle] = []
        self.running: Optional[Candle] = None
        self.on_close = on_close      # async (candle, history)
        self.on_update = on_update    # (candle, seconds_left)

    # ------------------------------------------------------------- feed
    def add_tick(self, price: float, ts: float):
        minute = int(ts // 60) * 60
        sec_into = ts - minute

        if self.running is None or minute > self.running.minute:
            prev = self.running
            if prev is not None:
                self._finalize(prev)
            self.running = Candle(minute=minute, pair=self.pair)
            self.running.add_tick(price, sec_into)
            if prev is not None:
                self._emit_close(prev)
            if self.on_update:
                self.on_update(self.running, 60 - sec_into)
            return

        self.running.add_tick(price, sec_into)
        if self.on_update:
            self.on_update(self.running, 60 - sec_into)

    def _finalize(self, c: Candle):
        c.closed = True
        self.candles.append(c)
        if len(self.candles) > self.MAX_HISTORY:
            self.candles = self.candles[-self.MAX_HISTORY:]

    def _emit_close(self, c: Candle):
        if self.on_close:
            self.on_close(c, list(self.candles))

    # ------------------------------------------------------------- seed
    def seed_history(self, history: List):
        """Bootstrap from tick history [[ts, price, flag], ...] (fast-forward)."""
        self.candles = []
        self.running = None
        last_min = -1
        closed_pending = None
        for ts, price, _flag in history:
            self.add_tick(float(price), float(ts))
        # ensure last candle exists
        if self.running is None:
            now = time.time()
            self.running = Candle(minute=int(now // 60) * 60, pair=self.pair)

    def seed_server_candles(self, rows: List) -> int:
        """Merge server-computed M1 OHLC rows from history/list/v2.

        Row format (newest-first as delivered by Quotex):
            [minute, open, close, high, low, ticks, last_tick_ts]

        Server rows are authoritative for their minutes — this is the exact
        data the Quotex terminal chart draws, so seeding from it guarantees a
        1:1 match with the broker chart. Rules:
          * rows with minute < current minute  -> closed candles (merge/refresh
            by minute; local micro tick-stats are preserved when we already
            have that minute from live collection)
          * row with minute == current minute  -> running candle (widen local
            high/low, take server open/close/ticks)
          * local candles NEWER than the batch are always kept (live-collected)
        No close events fire from seeding, so no duplicate signals. Returns
        the number of minutes whose data changed (0 = nothing new)."""
        now_minute = int(time.time() // 60) * 60
        incoming = {}
        for r in rows:
            try:
                minute = int(r[0])
                o, c, h, l = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                ticks = int(r[5]) if len(r) > 5 and r[5] else 0
            except (ValueError, TypeError, IndexError):
                continue
            if minute > now_minute:
                continue                      # clock-skew guard: never accept future
            incoming[minute] = (o, h, l, c, ticks)
        if not incoming:
            return 0

        local = {c.minute: c for c in self.candles}
        changed = 0
        for minute, (o, h, l, c, ticks) in incoming.items():
            if minute == now_minute:
                continue                      # handled below as running candle
            lc = local.get(minute)
            if lc is not None:
                if (lc.open, lc.high, lc.low, lc.close) != (o, h, l, c):
                    # refresh from authoritative server values, keep local micro stats
                    lc.open, lc.high, lc.low, lc.close = o, h, l, c
                    lc.ticks = max(lc.ticks, ticks)
                    changed += 1
            else:
                nc = Candle(minute=minute, pair=self.pair,
                            open=o, high=h, low=l, close=c, ticks=ticks)
                nc.closed = True
                local[minute] = nc
                changed += 1
        self.candles = [local[m] for m in sorted(local)][-self.MAX_HISTORY:]

        # running candle row (current minute) — server may know more than us
        run_row = incoming.get(now_minute)
        if run_row is not None:
            o, h, l, c, ticks = run_row
            if self.running is None or self.running.minute != now_minute:
                self.running = Candle(minute=now_minute, pair=self.pair,
                                      open=o, high=h, low=l, close=c, ticks=ticks)
            else:
                run = self.running
                run.open = o                       # server open beats our partial one
                run.high = max(run.high, h)       # widen only — never shrink live H/L
                run.low = min(run.low, l)
                run.close = c                     # server close == latest tick
                run.ticks = max(run.ticks, ticks)
        elif self.running is None:
            self.running = Candle(minute=now_minute, pair=self.pair)
        return changed

    # ------------------------------------------------------------- info
    @property
    def seconds_left(self) -> int:
        if self.running is None:
            return 60
        return max(0, 60 - (time.time() - self.running.minute))

    def micro_snapshot(self) -> Dict:
        """Live micro-structure of the running candle (for last-10s meter)."""
        c = self.running
        if c is None:
            return {"seconds_left": 60, "bias": 0.0, "flip_risk": False,
                    "body_pp": 0.0, "dir": 0}
        sec_left = max(0, 60 - (time.time() - c.minute))
        bias = c.last10_bias if sec_left <= 10 else c.delta_norm
        open_ = c.open
        # flip risk: tiny body + opposite late momentum
        body_abs = abs(c.close - open_)
        ref = (c.high - c.low) or 1e-12
        flip = (body_abs / ref) < 0.18 and c.ticks > 12 and abs(bias) > 0.5 and sec_left <= 12
        return {
            "seconds_left": int(sec_left),
            "bias": round(bias, 3),
            "flip_risk": bool(flip),
            "body_pp": round((c.close - open_) / max(abs(open_), 1e-12) * 10000, 2),
            "dir": c.direction,
            "ticks": c.ticks,
            "close_pos": round(c.close_pos, 3),
        }

    def snapshot(self) -> Dict:
        c = self.running
        return {
            "pair": self.pair,
            "running": c.to_dict() if c else None,
            "seconds_left": self.seconds_left,
            "micro": self.micro_snapshot(),
        }
