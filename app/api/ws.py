"""WebSocket hub: fan-out live data to all connected browsers.

Design (zero head-of-line blocking):
  * every client gets its OWN outbound queue + sender task — one slow
    browser can never stall the others (previously a single shared pump
    awaited sends sequentially, which froze candles for everyone)
  * each queue is bounded; when full the OLDEST frame is dropped so a
    slow client skips stale frames instead of lagging minutes behind
  * messages are serialized ONCE and reused for all clients
  * the last 'status' and the last 'history' per pair are cached and
    replayed to late joiners, so a freshly opened chart shows candles
    immediately, before any REST fetch completes
"""
import asyncio
import json
import logging
from typing import Dict, List, Optional

from fastapi import WebSocket

log = logging.getLogger("hub")


class _Outbound:
    """Per-client sender: drains its own queue, dies silently on disconnect."""

    QUEUE_MAX = 900          # ~75s of tick traffic per pair; drop-oldest beyond

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self.task: Optional[asyncio.Task] = None
        self.dropped = 0
        self.sent = 0

    def start(self):
        self.task = asyncio.create_task(self._run(), name="hub-outbound")

    def push(self, text: str):
        try:
            self.q.put_nowait(text)
        except asyncio.QueueFull:
            # drop the oldest frame and push the newest — a slow client
            # stays in sync with "now" instead of falling further behind
            try:
                self.q.get_nowait()
                self.dropped += 1
            except Exception:
                pass
            try:
                self.q.put_nowait(text)
            except Exception:
                pass

    def push_front(self, text: str):
        """Bypass the queue (used only for the attach-time replay cache)."""
        self.q.put_nowait(text)

    async def _run(self):
        try:
            while True:
                text = await self.q.get()
                await self.ws.send_text(text)
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # socket died — hub detaches on its receive side / next failure
            pass

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass


class Hub:
    def __init__(self):
        self.clients: Dict[WebSocket, _Outbound] = {}
        self.on_attach = None            # callback (async) to refresh status
        self.last_status: Dict = {}
        self.last_history: Dict[str, Dict] = {}   # pair -> last history msg

    # ----------------------------------------------------------- lifecycle
    async def start(self):
        pass                              # senders are per-client, nothing to pump

    async def stop(self):
        for out in list(self.clients.values()):
            await out.stop()
        self.clients.clear()

    # ----------------------------------------------------------- producers
    def broadcast(self, msg: dict):
        """Serialize once, fan out to every client's own queue (never blocks)."""
        try:
            mtype = msg.get("type")
            if mtype == "status":
                self.last_status = msg
            elif mtype == "history":
                pair = msg.get("pair")
                if pair:
                    self.last_history[pair] = msg
            text = json.dumps(msg)
        except Exception:
            return
        for out in list(self.clients.values()):
            out.push(text)

    def broadcast_nowait(self, msg: dict):
        self.broadcast(msg)               # sync now — put_nowait only, no task

    # ----------------------------------------------------------- consumers
    async def attach(self, ws: WebSocket):
        await ws.accept()
        out = _Outbound(ws)
        out.start()
        self.clients[ws] = out
        if self.on_attach:
            try:
                await self.on_attach()
            except Exception:
                pass
        # replay cache for late joiners: last status + last history per pair —
        # a chart opened seconds ago still fills instantly on a fresh tab
        replay: List[str] = []
        if self.last_status:
            replay.append(json.dumps(self.last_status))
        for msg in self.last_history.values():
            replay.append(json.dumps(msg))
        for text in replay:
            try:
                await ws.send_text(text)
            except Exception:
                break

    def detach(self, ws: WebSocket):
        out = self.clients.pop(ws, None)
        if out:
            asyncio.get_event_loop().create_task(out.stop())
