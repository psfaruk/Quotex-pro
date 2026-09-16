"""Tracker: opens signals, settles WIN/LOSS at next candle close, keeps stats hot.

Settlement correctness for candle parity: the provisional result is computed
the instant a candle closes from the live tick stream; seconds later the
periodic server-candle resync may publish the authoritative OHLC for that
minute. reconcile() re-settles any signal whose close price changed and
broadcasts the corrected result — so reported WIN/LOSS always agrees with
the candle the Quotex terminal actually drew."""
import time
import logging
from collections import OrderedDict
from typing import Dict, Optional

from ..models import Candle, Signal
from ..storage import db

log = logging.getLogger("tracker")


class Tracker:
    SETTLED_CACHE = 240            # (pair, minute) -> settled signal, LRU

    def __init__(self, hub):
        self.hub = hub
        self.pending: Dict[str, Signal] = {}
        self._settled: "OrderedDict[tuple, Signal]" = OrderedDict()

    async def open(self, sig: Signal) -> int:
        sig_id = await db.insert_signal(sig)
        sig.id = sig_id
        self.pending[sig.pair] = sig
        return sig_id

    @staticmethod
    def _classify(direction: str, entry: float, close: float) -> str:
        diff = close - entry
        if abs(diff) < 1e-12:
            return "TIE"
        if (diff > 0 and direction == "CALL") or (diff < 0 and direction == "PUT"):
            return "WIN"
        return "LOSS"

    async def settle(self, pair: str, closed: Candle) -> Optional[Signal]:
        sig = self.pending.pop(pair, None)
        if sig is None:
            return None
        result = self._classify(sig.direction, sig.entry, closed.close)
        sig.close = closed.close
        sig.result = result
        sig.settled_at = time.time()
        await db.settle_signal(sig.id, closed.close, result, sig.settled_at)
        self._remember(sig)
        self.hub.broadcast({
            "type": "result", "pair": pair, "signal": sig.to_dict(),
            "candle": closed.to_dict(),
        })
        log.info("RESULT %s %s %s entry=%.5f close=%.5f", pair, sig.direction, result, sig.entry, closed.close)
        return sig

    def _remember(self, sig: Signal):
        key = (sig.pair, sig.candle_minute)
        self._settled[key] = sig
        while len(self._settled) > self.SETTLED_CACHE:
            self._settled.popitem(last=False)

    async def reconcile(self, pair: str, corrections: list) -> int:
        """Re-settle signals whose candle close was corrected by the server.

        corrections: [{minute, old_close, new_close}, ...] produced by
        CandleEngine.seed_server_candles. Only minutes that flip a result are
        re-written; the browser gets a corrected 'result' frame."""
        if not corrections:
            return 0
        fixed = 0
        for cor in corrections:
            minute = int(cor.get("minute", 0))
            new_close = cor.get("new_close")
            sig = self._settled.get((pair, minute))
            if sig is None or new_close is None or sig.close == new_close:
                continue
            old_result = sig.result
            sig.close = new_close
            sig.result = self._classify(sig.direction, sig.entry, new_close)
            if sig.result == old_result:
                continue
            await db.settle_signal(sig.id, new_close, sig.result,
                                   sig.settled_at or time.time())
            fixed += 1
            self.hub.broadcast({
                "type": "result", "pair": pair, "signal": sig.to_dict(),
                "candle": {"minute": minute, "pair": pair, "close": new_close},
                "corrected": True,
            })
            log.info("RESULT-CORRECTED %s %s %s->%s close=%.5f->%.5f",
                     pair, sig.direction, old_result, sig.result,
                     cor.get("old_close", 0.0), new_close)
        return fixed

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
