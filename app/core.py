"""Core wiring: feed -> candle engines -> signal engine -> tracker -> hub."""
import asyncio
import logging
import time
from collections import deque
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
        # tick-rate telemetry: per-pair timestamp log (rolling ~15s window)
        self._ticklog: Dict[str, deque] = {}
        self._tps_cache: Dict[str, float] = {}          # pair -> cached t/s
        self._tps_at: Dict[str, float] = {}             # pair -> last compute time

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
        # request boot history (live feed) slightly after connect; the server
        # answers with ~198 server-computed candles per pair -> chart opens
        # with full history that matches Quotex exactly
        if self.feed_kind == "live":
            async def boot():
                await asyncio.sleep(6)
                for p in pairs:
                    try:
                        await self.feed.request_history(p)
                    except Exception as e:
                        log.warning("boot history %s failed: %s", p, e)
            asyncio.create_task(boot())
        else:
            async def boot_demo():
                await asyncio.sleep(1)
                for p in pairs:
                    try:
                        await self.feed.request_history(p)
                    except Exception as e:
                        log.warning("boot history %s failed: %s", p, e)
            asyncio.create_task(boot_demo())

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
        log_ = self._ticklog.get(pair)
        if log_ is None:
            log_ = self._ticklog[pair] = deque(maxlen=200)
        log_.append(ts if ts > 1e9 else time.time())
        eng.add_tick(price, ts)

    def _tps(self, pair: str) -> float:
        """Rolling ticks-per-second over the last ~8s (recomputed at 2Hz max)."""
        now = time.time()
        if now - self._tps_at.get(pair, 0.0) < 0.5:
            return self._tps_cache.get(pair, 0.0)
        self._tps_at[pair] = now
        log_ = self._ticklog.get(pair)
        if not log_:
            self._tps_cache[pair] = 0.0
            return 0.0
        cutoff = now - 8.0
        n = 0
        for t in reversed(log_):
            if t >= cutoff:
                n += 1
            else:
                break
        self._tps_cache[pair] = round(n / 8.0, 1)
        return self._tps_cache[pair]

    def _on_candle_update(self, pair: str, candle, seconds_left: float):
        # EVERY tick is forwarded — no throttle. Quotex delivers ~8-12
        # ticks/sec per pair and the browser applies each one instantly
        # (sub-millisecond hop from socket to series.update()).
        self.hub.broadcast_nowait({
            "type": "candle_update", "pair": pair,
            "candle": candle.to_dict() if candle.ticks > 0 else None,
            "seconds_left": round(max(0.0, seconds_left), 2),
            "price": self.price.get(pair),
            "micro": self.engines[pair].micro_snapshot(),
            "tps": self._tps(pair),
            "ts": round(time.time(), 3),
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

    def _on_history(self, pair: str, data):
        """history/list/v2 payload arrived (server re-pushes on refresh).

        The payload carries BOTH:
          * "candles": server-computed M1 OHLC rows (~198 candles) — the exact
            data the Quotex terminal draws. Preferred: guaranteed 1:1 match
            with the broker chart + instant full history on chart open.
          * "history": raw ticks (~500s) — legacy fallback when the server
            variant sends no candle rows (aggregated locally instead)."""
        eng = self.engines.get(pair)
        if eng is None or not isinstance(data, dict):
            return
        rows = data.get("candles") or []
        ticks = data.get("history") or []
        changed = 0
        if rows:
            try:
                changed = eng.seed_server_candles(rows)
            except Exception as e:
                log.warning("server-candle seed %s failed: %s", pair, e)
            if changed:
                # persist authoritative history immediately (backtest + instant
                # pair-switch restore from DB on next boot)
                asyncio.get_event_loop().create_task(self._persist_engine_candles(pair, eng))
        elif ticks and len(eng.candles) < 3:
            # legacy fallback: build ~8 candles locally from raw ticks
            try:
                eng.seed_history(ticks)
                changed = 1
            except Exception as e:
                log.warning("tick seed %s failed: %s", pair, e)
        if changed:
            self.hub.broadcast_nowait({
                "type": "history", "pair": pair,
                "candles": [c.to_dict() for c in eng.candles[-200:]],
                "running": eng.running.to_dict() if (eng.running and eng.running.ticks > 0) else None,
                "seconds_left": eng.seconds_left,
            })
            asyncio.get_event_loop().create_task(self._broadcast_status())
            log.info("merged %d server candles for %s (engine now holds %d)",
                     changed, pair, len(eng.candles))

    async def refresh_history(self, pair: str) -> Dict:
        """Re-request server history for a pair (pair switch / manual resync).

        Live feed: chart_notification/get -> history/list/v2 with ~198
        server-computed candles; DemoFeed: simulated equivalent. The arriving
        payload seeds the engine and is broadcast to every browser, so the
        chart for this pair refills with authoritative candles immediately."""
        if self.feed is None:
            return {"pair": pair, "candles": [], "running": None,
                    "seconds_left": 60, "micro": {}, "price": None}
        try:
            await self.feed.request_history(pair)
        except Exception as e:
            log.warning("refresh_history %s failed: %s", pair, e)
        # _on_history callback seeds the engine + broadcasts async; give the
        # loop one beat so the broadcast lands before we answer REST callers
        await asyncio.sleep(0)
        return await self.candles_payload(pair, 200)

    async def _persist_engine_candles(self, pair: str, eng):
        """Persist the engine's closed candles to SQLite (REPLACE by minute)."""
        try:
            rows = [(
                c.pair, c.minute, c.open, c.high, c.low, c.close,
                c.ticks, c.up_ticks, c.down_ticks, c.last10_up, c.last10_down,
            ) for c in eng.candles[-200:]]
            if rows:
                await db.save_candles(rows)
        except Exception as e:
            log.warning("persist candles %s failed: %s", pair, e)

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
                "tps": self._tps(p),
            })
        self.hub.broadcast({
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
            "ts": round(time.time(), 3),
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
    async def candles_payload(self, pair: str, limit: int = 120) -> Dict:
        """Closed candles for a pair: DB history merged with live engine memory.

        The engine only keeps candles accumulated since boot (plus a short
        history seed); the DB holds everything ever persisted. Merging both
        means switching pairs always shows the full available history,
        immediately, together with the live running candle."""
        eng = self.engines.get(pair)
        by_minute: Dict[int, Dict] = {}
        # 1) persisted history (older runs / earlier candles)
        try:
            for r in await db.load_candles(pair, limit):
                by_minute[int(r["minute"])] = {
                    "minute": int(r["minute"]), "pair": pair,
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "ticks": r["ticks"],
                    "up": r["up"], "down": r["down"],
                    "l10u": r["l10u"], "l10d": r["l10d"],
                }
        except Exception as e:
            log.warning("load_candles(%s) failed: %s", pair, e)
        # 2) live engine candles are authoritative for their minutes
        if eng:
            for c in eng.candles[-limit:]:
                by_minute[c.minute] = c.to_dict()
        closed = [by_minute[mn] for mn in sorted(by_minute)][-limit:]
        running = eng.running.to_dict() if (eng and eng.running and eng.running.ticks > 0) else None
        return {
            "pair": pair, "candles": closed, "running": running,
            "seconds_left": eng.seconds_left if eng else 60,
            "micro": eng.micro_snapshot() if eng else {},
            "price": self.price.get(pair),
            "tps": self._tps(pair),
        }


core = AppCore()
