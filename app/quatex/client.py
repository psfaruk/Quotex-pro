"""
Quotex WebSocket client (unofficial).

Protocol (reverse-engineered & live-verified):
  URL:     wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket
  Headers: Origin/Referer https://qxbroker.com + Chrome UA
  Flow:    recv '0{sid}' -> send '40' -> recv '40' ->
           send 42["authorization",{"session":TOKEN,"isDemo":1,"tournamentId":0}] ->
           recv s_authorization -> send 451-["instruments/list",...] ->
           per pair: 42["instruments/follow","PAIR"],
                     42["instruments/update",{"asset":"PAIR","period":60}],
                     42["chart_notification/get",{"asset":"PAIR","version":"1.0.0"}]
  EIO=3:   client sends '2' ping every ~18s.
  Binary:  '451-["event",{"_placeholder":true,"num":0}]' then a BINARY frame:
           payload = b'\\x04' + JSON.
  Ticks:   quotes/stream -> [[pair, ts, price, flag], ...]
  History: history/list/v2 -> {"asset":..,"period":60,"history":[[ts,price,flag],..]}
"""
import asyncio
import json
import logging
import random
import time
from typing import Callable, Dict, List, Optional, Any

import websockets

log = logging.getLogger("quatex")

URL = "wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Origin": "https://qxbroker.com",
    "Referer": "https://qxbroker.com/",
    "Accept-Language": "en-US,en;q=0.9",
}


class QuotexClient:
    """Live Quotex feed. Calls user callbacks; auto-reconnects forever."""

    def __init__(self, token: str, is_demo: int, pairs: List[str]):
        self.token = token
        self.is_demo = is_demo
        self.pairs = pairs
        self.connected = False
        self.authenticated = False
        self.balance: Dict[str, Any] = {}
        self.instruments: List[Any] = []
        self.payouts: Dict[str, int] = {}

        # callbacks
        self.on_tick: Callable = None            # (pair, ts, price)
        self.on_history: Callable = None         # (pair, history[[ts,price,flag]])
        self.on_status: Callable = None          # (dict)
        self.on_balance: Callable = None         # (dict)

        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_event = "?"
        self._followed = False
        self._history_waiters: Dict[str, asyncio.Future] = {}
        # auth failure state: set when server explicitly rejects the token
        self.auth_error: str = ""          # "TOKEN_REJECTED" | "" (machine-readable)
        self._reject_count = 0

    # ---------------------------------------------------------------- public
    def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="quatex-client")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def set_pairs(self, pairs: List[str]):
        self.pairs = pairs
        # re-follow on next reconnect; also try immediately if connected
        if self.connected and self._ws:
            asyncio.create_task(self._follow_all())

    async def request_history(self, pair: str) -> List:
        """Request tick history for a pair (returns awaitable result)."""
        if not (self.connected and self._ws):
            return []
        fut = asyncio.get_event_loop().create_future()
        self._history_waiters[pair] = fut
        try:
            await self._send(f'42["chart_notification/get",{{"asset":"{pair}","version":"1.0.0"}}]')
            return await asyncio.wait_for(fut, 12)
        except asyncio.TimeoutError:
            return []
        finally:
            self._history_waiters.pop(pair, None)

    # ----------------------------------------------------------------- loop
    async def _run_loop(self):
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("quotex session ended: %s", e)
                self.connected = False
                self.authenticated = False
                # keep auth_error sticky so the UI keeps explaining WHY
                self._emit_status(error=self.auth_error or str(e))
            # rejected token will not heal by hammering the server:
            # back off hard (60s + jitter). A NEW token triggers apply_settings
            # which stops this task and starts a fresh client immediately.
            if self.auth_error == "TOKEN_REJECTED":
                await asyncio.sleep(60 + random.random() * 15)
            else:
                await asyncio.sleep(min(30, 3 + random.random() * 4))

    async def _session(self):
        # per-session reset: previous reject is re-tested with this attempt
        self.auth_error = ""
        connect = websockets.connect(URL, additional_headers=HEADERS,
                                     ping_interval=None, ping_timeout=None)
        try:
            self._ws = await asyncio.wait_for(connect, 15)
        except TypeError:
            connect = websockets.connect(URL, extra_headers=HEADERS,
                                         ping_interval=None, ping_timeout=None)
            self._ws = await asyncio.wait_for(connect, 15)
        self.connected = True
        self._followed = False
        self._emit_status()
        try:
            await self._handshake_and_auth()
            ping = asyncio.create_task(self._pinger())
            try:
                await self._read_loop()
            finally:
                ping.cancel()
        finally:
            self.connected = False
            self.authenticated = False
            try:
                await self._ws.close()
            except Exception:
                pass
            # after a reject the server closes the socket right away —
            # keep the auth_error visible so the UI can explain it
            self._emit_status(error=self.auth_error)

    async def _pinger(self):
        while True:
            await asyncio.sleep(18)
            try:
                await self._send("2")
            except Exception:
                return

    async def _send(self, msg: str):
        if self._ws:
            await self._ws.send(msg)

    async def _handshake_and_auth(self):
        ws = self._ws
        # 0{sid}
        raw = await asyncio.wait_for(ws.recv(), 10)
        if isinstance(raw, str) and raw.startswith("0"):
            await self._send("40")
        # 40 (namespace ack)
        deadline = time.time() + 8
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), 8)
            if isinstance(raw, str) and raw == "2":
                await self._send("3")
                continue
            if isinstance(raw, str) and raw.startswith("40"):
                break
        auth = (f'42["authorization",{{"session":"{self.token}",'
                f'"isDemo":{self.is_demo},"tournamentId":0}}]')
        await self._send(auth)

    async def _read_loop(self):
        ws = self._ws
        while True:
            raw = await ws.recv()
            if isinstance(raw, (bytes, bytearray)):
                await self._handle_binary(bytes(raw))
                continue
            if raw == "2":
                await self._send("3")
                continue
            if raw == "3":
                continue
            if raw.startswith("0"):
                await self._send("40")
                continue
            if raw.startswith("40"):
                continue
            if raw.startswith("451-"):
                try:
                    self._last_event = json.loads(raw[4:])[0]
                except Exception:
                    pass
                if self._last_event == "s_authorization":
                    self.authenticated = True
                    self._emit_status()
                    await self._send('451-["instruments/list",{"_placeholder":true,"num":0}]')
                    await asyncio.sleep(0.5)
                    if not self._followed:
                        await self._follow_all()
                continue
            if raw.startswith("42"):
                try:
                    payload = json.loads(raw[2:])
                    ev = payload[0] if isinstance(payload, list) and payload else "?"
                    data = payload[1] if isinstance(payload, list) and len(payload) > 1 else None
                except Exception:
                    continue
                if ev in ("s_authorization", "authenticated"):
                    self.authenticated = True
                    self._emit_status()
                    await self._send('451-["instruments/list",{"_placeholder":true,"num":0}]')
                    if not self._followed:
                        await self._follow_all()
                elif ev in ("authorization/reject", "authorization-fail", "s_authorization/reject"):
                    # server explicitly refused our session token (expired / invalid /
                    # logged-out elsewhere). Surface it loudly instead of silent retry.
                    self.auth_error = "TOKEN_REJECTED"
                    self._reject_count += 1
                    log.warning("quotex authorization REJECTED (token expired/invalid, attempt %d)",
                                self._reject_count)
                    self._emit_status(error="TOKEN_REJECTED")
                elif ev == "successauth":
                    self.authenticated = True
                    self._emit_status()
            if raw == "41":
                # namespace disconnect — usually follows a reject; read_loop will end
                # on the socket close right after
                continue

    async def _handle_binary(self, payload: bytes):
        if payload[:1] == b"\x04":
            payload = payload[1:]
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        ev = self._last_event
        if ev == "quotes/stream" and isinstance(data, list):
            for tick in data:
                if isinstance(tick, list) and len(tick) >= 3:
                    try:
                        pair, ts, price = str(tick[0]), float(tick[1]), float(tick[2])
                        if self.on_tick:
                            self.on_tick(pair, ts, price)
                    except (ValueError, TypeError):
                        pass
        elif ev == "history/list/v2" and isinstance(data, dict):
            pair = data.get("asset")
            hist = data.get("history") or []
            fut = self._history_waiters.get(pair)
            if fut and not fut.done():
                fut.set_result(hist)
            if self.on_history and hist:
                self.on_history(pair, hist)
        elif ev == "instruments/list" and isinstance(data, list):
            self.instruments = data
            for it in data:
                if isinstance(it, list) and len(it) > 5 and isinstance(it[1], str):
                    self.payouts[it[1]] = it[5]
            self._emit_status()
        elif ev == "s_balance/list" and isinstance(data, dict):
            self.balance = data
            self._emit_status()
            if self.on_balance:
                self.on_balance(data)

    async def _follow_all(self):
        self._followed = True
        for p in self.pairs:
            try:
                await self._send(f'42["instruments/follow","{p}"]')
                await self._send(f'42["instruments/update",{{"asset":"{p}","period":60}}]')
                await self._send(f'42["chart_notification/get",{{"asset":"{p}","version":"1.0.0"}}]')
                await asyncio.sleep(0.25)
            except Exception:
                return

    def _emit_status(self, error: str = ""):
        if self.on_status:
            self.on_status({
                "feed": "live", "connected": self.connected,
                "authenticated": self.authenticated, "error": error,
                "auth_error": self.auth_error,
                "balance": self.balance, "payouts": self.payouts,
                "broker": "Quotex", "mode": "demo" if self.is_demo else "real",
            })


class DemoFeed:
    """Synthetic tick feed used when no token is configured.

    Simulates a mean-reverting random walk with momentum regimes so the
    whole app (candles, signals, stats, chart) stays demonstrable offline.
    Clearly labeled feed='demo' in the UI."""

    TICK_HZ = 4.0

    def __init__(self, pairs: List[str]):
        self.pairs = pairs
        self.connected = True
        self.authenticated = True
        self.balance = {"liveBalance": 0, "demoBalance": 10000}
        self.instruments = []
        self.payouts = {p: 92 for p in pairs}
        self.on_tick: Callable = None
        self.on_history: Callable = None
        self.on_status: Callable = None
        self.on_balance: Callable = None
        self._task = None
        self._prices = {
            "EURUSD_otc": 1.0850, "EURUSD": 1.0850,
            "USDJPY_otc": 154.50, "USDJPY": 154.50,
            "AUDUSD_otc": 0.7050, "AUDUSD": 0.7050,
            "GBPUSD_otc": 1.2680, "GBPUSD": 1.2680,
            "EURJPY_otc": 167.20, "GBPJPY_otc": 196.30,
            "USDCAD_otc": 1.3520, "USDCHF_otc": 0.8830,
            "EURGBP_otc": 0.8550, "NZDUSD_otc": 0.6150,
            "AUDCAD_otc": 0.9530, "AUDJPY_otc": 108.90,
            "CADCHF_otc": 0.6530, "CHFJPY_otc": 174.90, "EURAUD_otc": 1.5390,
        }
        self._drift = {p: 0.0 for p in pairs}
        self._vol = {p: 0.00022 for p in pairs}
        self._stop = asyncio.Event()

    def _default_price(self, p: str) -> float:
        base = p.replace("_otc", "")
        defaults = {"EURUSD": 1.0850, "USDJPY": 154.50, "AUDUSD": 0.7050, "GBPUSD": 1.2680,
                    "EURJPY": 167.20, "GBPJPY": 196.30, "USDCAD": 1.3520, "USDCHF": 0.8830,
                    "EURGBP": 0.8550, "NZDUSD": 0.6150, "AUDCAD": 0.9530, "AUDJPY": 108.90,
                    "CADCHF": 0.6530, "CHFJPY": 174.90, "EURAUD": 1.5390}
        return defaults.get(base, 1.0)

    def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="demo-feed")
        if self.on_status:
            self.on_status({
                "feed": "demo", "connected": True, "authenticated": True,
                "error": "", "balance": self.balance, "payouts": self.payouts,
                "broker": "Demo Simulator", "mode": "demo",
            })

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def set_pairs(self, pairs: List[str]):
        self.pairs = pairs
        for p in pairs:
            self._prices.setdefault(p, self._default_price(p))
            self._drift.setdefault(p, 0.0)
            self._vol.setdefault(p, 0.00022)

    async def request_history(self, pair: str) -> List:
        """Generate ~9 minutes of backfill ticks."""
        now = time.time()
        start = now - 540
        price = self._prices.get(pair, self._default_price(pair))
        self._prices[pair] = price
        step = 1.0 / self.TICK_HZ
        hist = []
        t = start
        drift = 0.0
        while t < now:
            if random.random() < 0.004:
                drift = random.uniform(-3, 3) * 1e-5
            if random.random() < 0.01:
                drift = -0.7 * drift
            price *= (1 + drift + random.gauss(0, 1.1e-4))
            hist.append([round(t, 3), round(price, 5), random.randint(0, 1)])
            t += step
        if self.on_history and hist:
            self.on_history(pair, hist)
        return hist

    async def _run(self):
        while not self._stop.is_set():
            try:
                now = time.time()
                for p in self.pairs:
                    price = self._prices.get(p, self._default_price(p))
                    # regime shift
                    if random.random() < 0.006:
                        self._drift[p] = random.uniform(-3, 3) * 1e-5
                    # mean reversion pull
                    if random.random() < 0.015:
                        self._drift[p] = -0.7 * self._drift[p]
                    price *= (1 + self._drift[p] + random.gauss(0, 1.1e-4))
                    self._prices[p] = price
                    if self.on_tick:
                        self.on_tick(p, round(now, 3), round(price, 5))
                await asyncio.sleep(1.0 / self.TICK_HZ)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)
