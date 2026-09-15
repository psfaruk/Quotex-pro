"""WebSocket hub: fan-out live data to all connected browsers."""
import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket

log = logging.getLogger("hub")


class Hub:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
        self._task = None
        self.last_status = {}
        self.on_attach = None    # callback(pair=core) to refresh status

    # ----------------------------------------------------------- lifecycle
    async def start(self):
        self._task = asyncio.create_task(self._pump(), name="hub-pump")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ----------------------------------------------------------- producers
    async def broadcast(self, msg: dict):
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(msg)
            except Exception:
                pass

    def broadcast_nowait(self, msg: dict):
        asyncio.get_event_loop().create_task(self.broadcast(msg))

    # ----------------------------------------------------------- consumers
    async def attach(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)
        if self.on_attach:
            try:
                await self.on_attach()
            except Exception:
                pass
        # snapshot for late joiners
        if self.last_status:
            try:
                await ws.send_text(json.dumps(self.last_status))
            except Exception:
                pass

    def detach(self, ws: WebSocket):
        self.clients.discard(ws)

    async def _pump(self):
        while True:
            msg = await self._queue.get()
            if msg.get("type") == "status":
                self.last_status = msg
            if not self.clients:
                continue
            text = json.dumps(msg)
            dead = []
            for ws in self.clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.detach(ws)
