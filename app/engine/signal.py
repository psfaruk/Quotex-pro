"""Signal engine: candle close -> confluence decision -> signal lifecycle."""
import time
import logging
from typing import Dict, List, Optional

from ..models import Candle, Signal
from .analysis import compute_factors

log = logging.getLogger("signal")


class SignalEngine:
    """Generates next-candle CALL/PUT predictions at each M1 close."""

    def __init__(self, settings, tracker, hub):
        self.settings = settings          # app.config.Settings (live ref)
        self.tracker = tracker
        self.hub = hub

    async def on_candle_close(self, pair: str, closed: Candle, history: List[Candle]):
        # 1) settle the previous pending signal for this pair (if any)
        await self.tracker.settle(pair, closed)

        # 2) score the fresh candle
        score, factors = compute_factors(closed, history, live=True)
        conf = int(round(50 + min(abs(score), 120.0) * 0.375))

        result = {
            "type": "analysis", "pair": pair, "minute": closed.minute,
            "score": round(score, 1), "confidence": conf, "factors": factors,
            "candle": closed.to_dict(),
        }

        threshold = self.settings.threshold
        min_conf = self.settings.min_confidence
        if abs(score) >= threshold and conf >= min_conf:
            direction = "CALL" if score > 0 else "PUT"
            sig = Signal(
                id=None, pair=pair, candle_minute=closed.minute + 60,
                direction=direction, score=score, confidence=conf,
                entry=closed.close, factors=factors, created_at=time.time(),
            )
            sig.id = await self.tracker.open(sig)
            self.hub.broadcast({
                **result, "signal": sig.to_dict(),
            })
            log.info("SIGNAL %s %s conf=%d score=%.0f", pair, direction, conf, score)
        else:
            result["signal"] = None
            result["reason"] = ("কনফ্লুয়েন্স অপর্যাপ্ত — NO SIGNAL"
                                if abs(score) < threshold else
                                f"কনফিডেন্স {conf} < {min_conf} — ফিল্টার্ড")
            self.hub.broadcast(result)
