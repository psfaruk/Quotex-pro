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
