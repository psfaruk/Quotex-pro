"""Tracker: opens signals, settles WIN/LOSS at next candle close, keeps stats hot."""
import time
import logging
from typing import Dict, Optional

from ..models import Candle, Signal
from ..storage import db

log = logging.getLogger("tracker")


class Tracker:
    def __init__(self, hub):
        self.hub = hub
        self.pending: Dict[str, Signal] = {}

    async def open(self, sig: Signal) -> int:
        sig_id = await db.insert_signal(sig)
        sig.id = sig_id
        self.pending[sig.pair] = sig
        return sig_id

    async def settle(self, pair: str, closed: Candle) -> Optional[Signal]:
        sig = self.pending.pop(pair, None)
        if sig is None:
            return None
        diff = closed.close - sig.entry
        if abs(diff) < 1e-12:
            result = "TIE"
        elif (diff > 0 and sig.direction == "CALL") or (diff < 0 and sig.direction == "PUT"):
            result = "WIN"
        else:
            result = "LOSS"
        sig.close = closed.close
        sig.result = result
        sig.settled_at = time.time()
        await db.settle_signal(sig.id, closed.close, result, sig.settled_at)
        self.hub.broadcast({
            "type": "result", "pair": pair, "signal": sig.to_dict(),
            "candle": closed.to_dict(),
        })
        log.info("RESULT %s %s %s entry=%.5f close=%.5f", pair, sig.direction, result, sig.entry, closed.close)
        return sig

    async def restore_pending(self):
        """On startup, re-arm unsettled signals from DB."""
        for row in await db.pending_signals():
            sig = Signal(
                id=row["id"], pair=row["pair"], candle_minute=row["minute"],
                direction=row["direction"], score=row["score"],
                confidence=row["confidence"], entry=row["entry"],
                close=row["close"], result=row["result"],
                created_at=row["created_at"], settled_at=row["settled_at"],
            )
            try:
                sig.factors = __import__("json").loads(row["factors"] or "[]")
            except Exception:
                sig.factors = []
            # stale pending (older than 5 min) will be voided by next settle check
            if time.time() - sig.created_at < 300:
                self.pending[sig.pair] = sig
