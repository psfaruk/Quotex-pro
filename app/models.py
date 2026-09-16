"""Core data models for QX Signal Pro."""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class Tick:
    pair: str
    ts: float          # unix seconds (float, sub-second precision)
    price: float
    flag: int = 0      # quote side marker from Quotex

    def to_dict(self):
        return {"pair": self.pair, "ts": self.ts, "price": self.price}


@dataclass
class Candle:
    minute: int                # unix seconds, floored to minute
    pair: str
    open: float = 0.0
    high: float = -1e18
    low: float = 1e18
    close: float = 0.0
    ticks: int = 0
    up_ticks: int = 0
    down_ticks: int = 0
    last10_up: int = 0         # up-ticks in final 10s (live feed only)
    last10_down: int = 0
    high_ts: float = 0.0       # when high was made (sec into candle)
    low_ts: float = 0.0        # when low was made
    last_ts: float = 0.0       # absolute unix ts of the LAST tick (freshness)
    closed: bool = False

    def add_tick(self, price: float, sec_into: float, abs_ts: float = 0.0):
        if self.ticks == 0:
            self.open = self.close = price
            self.high = self.low = price
            self.high_ts = self.low_ts = sec_into
        else:
            if price > self.high:
                self.high, self.high_ts = price, sec_into
            elif price < self.low:
                self.low, self.low_ts = price, sec_into
        if self.ticks > 0:
            if price > self.close:
                self.up_ticks += 1
            elif price < self.close:
                self.down_ticks += 1
            if sec_into >= 50.0:
                if price > self.close:
                    self.last10_up += 1
                elif price < self.close:
                    self.last10_down += 1
        self.close = price
        self.ticks += 1
        if abs_ts:
            self.last_ts = abs_ts

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-12)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_ratio(self) -> float:
        return self.body / self.range

    @property
    def direction(self) -> int:
        return 1 if self.close >= self.open else -1

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def close_pos(self) -> float:
        """0 = closed at low, 1 = closed at high."""
        return (self.close - self.low) / self.range

    @property
    def last10_bias(self) -> float:
        """Momentum bias of the final 10 seconds (-1..1)."""
        tot = self.last10_up + self.last10_down
        if tot == 0:
            return 0.0
        return (self.last10_up - self.last10_down) / tot

    @property
    def delta_norm(self) -> float:
        """Normalized up/down tick delta of whole candle (-1..1)."""
        tot = self.up_ticks + self.down_ticks
        if tot == 0:
            return 0.0
        return (self.up_ticks - self.down_ticks) / tot

    def to_dict(self):
        return {
            "minute": self.minute, "pair": self.pair,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "ticks": self.ticks, "up": self.up_ticks, "down": self.down_ticks,
            "l10u": self.last10_up, "l10d": self.last10_down,
        }


@dataclass
class Signal:
    id: Optional[int]
    pair: str
    candle_minute: int          # minute of the candle this signal predicts
    direction: str              # "CALL" | "PUT"
    score: float
    confidence: int             # 50..95
    entry: float                # price at candle close (= next candle open proxy)
    factors: List[Dict[str, Any]] = field(default_factory=list)
    close: Optional[float] = None
    result: Optional[str] = None    # "WIN" | "LOSS" | "TIE"
    created_at: float = 0.0
    settled_at: Optional[float] = None

    @property
    def tier(self) -> str:
        if self.confidence >= 78:
            return "strong"
        if self.confidence >= 63:
            return "medium"
        return "weak"

    def to_dict(self):
        return {
            "id": self.id, "pair": self.pair, "minute": self.candle_minute,
            "direction": self.direction, "score": round(self.score, 1),
            "confidence": self.confidence, "tier": self.tier,
            "entry": self.entry, "factors": self.factors,
            "close": self.close, "result": self.result,
            "created_at": self.created_at, "settled_at": self.settled_at,
        }
