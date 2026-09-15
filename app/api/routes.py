"""REST API routes."""
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

from ..config import KNOWN_PAIRS
from ..storage import db
from ..core import core
from ..backtest.runner import run_backtest

router = APIRouter()

WINDOWS = {"1h": 3600, "3h": 3 * 3600, "6h": 6 * 3600, "24h": 24 * 3600, "all": 10 * 365 * 86400}


@router.get("/status")
async def status():
    st = core.feed_status or {}
    pairs_info = []
    for p in core.settings.pairs:
        eng = core.engines.get(p)
        run = eng.running if eng else None
        pairs_info.append({
            "pair": p, "price": core.price.get(p),
            "seconds_left": int(eng.seconds_left) if eng else 60,
            "dir": run.direction if run else 0,
            "candles": len(eng.candles) if eng else 0,
            "payout": st.get("payouts", {}).get(p),
        })
    return {
        "feed": core.feed_kind, "connected": st.get("connected", False),
        "authenticated": st.get("authenticated", False),
        "broker": st.get("broker", ""), "mode": st.get("mode", ""),
        "error": st.get("error", ""), "auth_error": st.get("auth_error", ""),
        "balance": st.get("balance", {}),
        "pairs": pairs_info, "uptime": int(time.time() - core.started_at),
        "known_pairs": KNOWN_PAIRS,
        "pending": {p: s.to_dict() for p, s in core.tracker.pending.items()},
    }


@router.get("/candles/{pair}")
async def candles(pair: str, limit: int = 120):
    if limit < 10 or limit > 400:
        limit = 120
    return await core.candles_payload(pair, limit)


@router.get("/history/{pair}")
async def refresh_history(pair: str):
    """Re-pull authoritative server candles for a pair (pair-switch resync).

    Triggers feed.request_history -> engine seed -> 'history' broadcast to
    every connected browser, and also returns the merged payload directly
    (DB history + live engine memory + running candle)."""
    if pair not in KNOWN_PAIRS and pair not in core.engines:
        raise HTTPException(404, "unknown pair")
    return await core.refresh_history(pair)


@router.get("/signals")
async def signals(window: str = "1h", pair: Optional[str] = None,
                  direction: Optional[str] = None, result: Optional[str] = None,
                  limit: int = 300):
    if direction:
        direction = direction.upper()
        if direction not in ("CALL", "PUT"):
            raise HTTPException(400, "direction must be CALL|PUT")
    if result:
        result = result.upper()
        if result not in ("WIN", "LOSS", "TIE"):
            raise HTTPException(400, "result must be WIN|LOSS|TIE")
    w = WINDOWS.get(window, 3600)
    rows = await db.signals_window(w, pair, direction, result, limit)
    return {"window": window, "count": len(rows), "signals": rows}


@router.get("/stats")
async def stats(window: str = "1h"):
    w = WINDOWS.get(window, 3600)
    return await db.stats(w)


@router.get("/settings")
async def get_settings():
    return core.settings.masked()


class SettingsIn(BaseModel):
    token: Optional[str] = None
    is_demo: Optional[int] = None
    pairs: Optional[list] = None
    min_confidence: Optional[int] = None
    threshold: Optional[float] = None


@router.post("/settings")
async def post_settings(s: SettingsIn):
    kw = {k: v for k, v in s.dict().items()}
    if kw.get("pairs") is not None:
        kw["pairs"] = [p for p in kw["pairs"] if p in KNOWN_PAIRS]
        if not kw["pairs"]:
            raise HTTPException(400, "no valid pairs")
    return await core.apply_settings(**kw)


class BacktestIn(BaseModel):
    pair: Optional[str] = None
    limit: int = 400


@router.post("/backtest")
async def backtest(b: BacktestIn):
    return await run_backtest(core, b.pair, b.limit)


@router.get("/backtest/data")
async def backtest_data(pair: Optional[str] = None):
    """How much collected data is available for backtesting."""
    from ..storage import db as _db
    out = {}
    pairs = [pair] if pair else core.settings.pairs
    for p in pairs:
        rows = await _db.load_candles(p, 3000)
        out[p] = {"candles": len(rows),
                  "from": rows[0]["minute"] if rows else None,
                  "to": rows[-1]["minute"] if rows else None}
    return out


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await core.hub.attach(ws)
    try:
        while True:
            # client pings / subscription msgs (ignored for now)
            await ws.receive_text()
    except WebSocketDisconnect:
        core.hub.detach(ws)
    except Exception:
        core.hub.detach(ws)
