"""Core wiring: feed -> candle engines -> signal engine -> tracker -> hub."""
import asyncio
import logging
import time
from typing import Dict, List, Optional

from .config import Settings, KNOWN_PAIRS
from .storage import db
from .quatex.client import QuotexClient, DemoFeed
from .engine.candles import CandleEngine
from .engine.signal import SignalEngine
from .engine.tracker import Tracker
from .api.ws import Hub

log = logging.getLogger("core")


class AppCore:
    def __init__(self):
        self.settings: Settings = Settings()
        self.hub = Hub()
        self.tracker = Tracker(self.hub)
        self.signal_engine = SignalEngine(self.settings, self.tracker, self.hub)
        self.engines: Dict[str, CandleEngine] = {}
        self.feed = None                     # QuotexClient | DemoFeed
        self.feed_kind = "none"
        self.started_at = time.time()
        self.feed_status: Dict = {}
        self.price: Dict[str, float] = {}
        self._persist_task = None

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        raw = await db.get_setting("settings", "")
        self.settings = Settings.from_json(raw) if raw else Settings()
        from .config import Settings as S
        self.settings = S.with_env(self.settings)
        await self.hub.start()
        self.hub.on_attach = self._broadcast_status
        await self.tracker.restore_pending()
        self._persist_task = asyncio.create_task(self._persister())
        await self._start_feed()

    async def stop(self):
        if self.feed:
            await self.feed.stop()
        if self._persist_task:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.hub.stop()

    async def _start_feed(self):
        pairs = self.settings.pairs
        token = self.settings.token
        if token:
            self.feed = QuotexClient(token, self.settings.is_demo, pairs)
            self.feed_kind = "live"
        else:
            self.feed = DemoFeed(pairs)
            self.feed_kind = "demo"

        self.feed.on_tick = self._on_tick
        self.feed.on_history = self._on_history
        self.feed.on_status = self._on_feed_status
        self.feed.on_balance = lambda d: None
        self.engines.clear()
        for p in pairs:
            self._ensure_engine(p)
        self.feed.start()
        log.info("feed started: %s pairs=%s", self.feed_kind, pairs)
        # request boot history (live feed) slightly after connect
        if self.feed_kind == "live":
            async def boot():
                await asyncio.sleep(6)
                for p in pairs:
                    try:
                        await self.feed.request_history(p)
                    except Exception as e:
                        log.warning("boot history %s failed: %s", p, e)
            asyncio.create_task(boot())

    async def apply_settings(self, **kw) -> Dict:
        old = dict(token=self.settings.token, is_demo=self.settings.is_demo,
                   pairs=self.settings.pairs)
        self.settings.update(**kw)
        await db.set_setting("settings", self.settings.to_json())
        # decide what to restart
        need_restart = (
            kw.get("token") is not None and self.settings.token != old["token"]
        ) or (kw.get("is_demo") is not None and self.settings.is_demo != old["is_demo"])
        pairs_changed = self.settings.pairs != old["pairs"]
        if need_restart:
            if self.feed:
                await self.feed.stop()
            await self._start_feed()
        elif pairs_changed and self.feed:
            self.feed.set_pairs(self.settings.pairs)
            for p in self.settings.pairs:
                self._ensure_engine(p)
            if self.feed_kind == "live":
                for p in self.settings.pairs:
                    try:
                        await self.feed.request_history(p)
                    except Exception:
                        pass
        await self._broadcast_status()
        return self.settings.masked()

    # -------------------------------------------------------------- engine
    def _ensure_engine(self, pair: str) -> CandleEngine:
        if pair not in self.engines:
            eng = CandleEngine(
                pair,
                on_close=lambda c, h, p=pair: self._on_candle_close(p, c, h),
                on_update=lambda c, s, p=pair: self._on_candle_update(p, c, s),
            )
            self.engines[pair] = eng
        return self.engines[pair]

    def _on_tick(self, pair: str, ts: float, price: float):
        eng = self.engines.get(pair)
        if eng is None:
            return
        self.price[pair] = price
        eng.add_tick(price, ts)

    def _on_candle_update(self, pair: str, candle, seconds_left: float):
        # throttle broadcasts: every tick is too chatty for browsers
        now = time.time()
        last = getattr(self, "_last_upd", {}).get(pair, 0)
        if now - last < 0.7 and seconds_left > 3:
            return
        getattr(self, "_last_upd", {})[pair] = now
        self.hub.broadcast_nowait({
            "type": "candle_update", "pair": pair,
            "candle": candle.to_dict(), "seconds_left": int(seconds_left),
            "price": self.price.get(pair),
            "micro": self.engines[pair].micro_snapshot(),
        })

    def _on_candle_close(self, pair: str, closed, history):
        # persist candle row
        asyncio.get_event_loop().create_task(db.save_candles([(
            closed.pair, closed.minute, closed.open, closed.high, closed.low,
            closed.close, closed.ticks, closed.up_ticks, closed.down_ticks,
            closed.last10_up, closed.last10_down,
        )]))
        # signal engine is async
        asyncio.get_event_loop().create_task(
            self.signal_engine.on_candle_close(pair, closed, history))

    def _on_history(self, pair: str, history: List):
        eng = self.engines.get(pair)
        if eng is None or not history:
            return
        # seed only once per boot — re-seeding would wipe live-collected candles
        # and duplicate signals (server re-pushes history on session refresh)
        if len(eng.candles) >= 3:
            return
        try:
            eng.seed_history(history)
            self.hub.broadcast_nowait({
                "type": "history", "pair": pair,
                "candles": [c.to_dict() for c in eng.candles[-120:]],
            })
            asyncio.get_event_loop().create_task(self._broadcast_status())
            log.info("seeded %s with %d ticks -> %d candles", pair, len(history), len(eng.candles))
        except Exception as e:
            log.warning("seed %s failed: %s", pair, e)

    def _on_feed_status(self, status: Dict):
        self.feed_status = status
        asyncio.get_event_loop().create_task(self._broadcast_status())

    # -------------------------------------------------------------- status
    async def _broadcast_status(self):
        st = self.feed_status or {}
        pairs_info = []
        for p in self.settings.pairs:
            eng = self.engines.get(p)
            run = eng.running if eng else None
            pairs_info.append({
                "pair": p,
                "price": self.price.get(p),
                "seconds_left": int(eng.seconds_left) if eng else 60,
                "dir": run.direction if run else 0,
                "body_ratio": round(run.body_ratio, 2) if run and run.ticks > 3 else None,
                "candles": len(eng.candles) if eng else 0,
                "payout": st.get("payouts", {}).get(p),
            })
        await self.hub.broadcast({
            "type": "status",
            "feed": self.feed_kind,
            "connected": st.get("connected", False),
            "authenticated": st.get("authenticated", False),
            "broker": st.get("broker", ""),
            "mode": st.get("mode", ""),
            "error": st.get("error", ""),
            "auth_error": st.get("auth_error", ""),
            "balance": st.get("balance", {}),
            "payouts": st.get("payouts", {}),
            "pairs": pairs_info,
            "uptime": int(time.time() - self.started_at),
            "pending": {p: s.to_dict() for p, s in self.tracker.pending.items()},
        })

    async def _persister(self):
        """Persist candles periodically + prune + status heartbeat."""
        n = 0
        while True:
            await asyncio.sleep(30)
            n += 1
            try:
                if n % 20 == 0:
                    await db.prune_candles(keep_per_pair=2000)
                await self._broadcast_status()
            except Exception as e:
                log.warning("persister error: %s", e)

    # -------------------------------------------------------------- queries
    def candles_payload(self, pair: str, limit: int = 120) -> Dict:
        eng = self.engines.get(pair)
        closed = [c.to_dict() for c in (eng.candles[-limit:] if eng else [])]
        running = eng.running.to_dict() if eng and eng.running else None
        return {
            "pair": pair, "candles": closed, "running": running,
            "seconds_left": eng.seconds_left if eng else 60,
            "micro": eng.micro_snapshot() if eng else {},
            "price": self.price.get(pair),
        }


core = AppCore()
