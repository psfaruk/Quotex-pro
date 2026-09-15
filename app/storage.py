"""SQLite persistence: settings, closed candles, signals, stats queries."""
import aiosqlite
import json
import time
from typing import List, Dict, Any, Optional

DB_PATH = "data/qx.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS candles (
    pair TEXT, minute INTEGER, open REAL, high REAL, low REAL, close REAL,
    ticks INTEGER, up INTEGER, down INTEGER, l10u INTEGER, l10d INTEGER,
    PRIMARY KEY (pair, minute)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT, minute INTEGER, direction TEXT, score REAL, confidence INTEGER,
    entry REAL, close REAL, result TEXT, factors TEXT,
    created_at REAL, settled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair);
"""


class DB:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        import os
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "DB not initialized"
        return self._db

    # ---------------- settings ----------------
    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str):
        await self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.db.commit()

    # ---------------- candles ----------------
    async def save_candles(self, rows: List[tuple]):
        """rows: (pair, minute, o,h,l,c, ticks, up, down, l10u, l10d)"""
        if not rows:
            return
        await self.db.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        await self.db.commit()

    async def load_candles(self, pair: str, limit: int = 500) -> List[Dict[str, Any]]:
        async with self.db.execute(
                "SELECT * FROM candles WHERE pair=? ORDER BY minute DESC LIMIT ?", (pair, limit)) as cur:
            rows = await cur.fetchall()
        out = [dict(r) for r in rows]
        out.reverse()
        return out

    async def prune_candles(self, keep_per_pair: int = 2000):
        await self.db.execute(
            f"""DELETE FROM candles WHERE pair||minute NOT IN (
                SELECT pair||minute FROM (
                    SELECT pair, minute FROM candles
                    ORDER BY minute DESC
                ) GROUP BY pair LIMIT 0
            )""")  # placeholder no-op, pruning done below
        # simpler: per-pair prune
        async with self.db.execute("SELECT DISTINCT pair FROM candles") as cur:
            pairs = [r["pair"] for r in await cur.fetchall()]
        for p in pairs:
            async with self.db.execute(
                    "SELECT minute FROM candles WHERE pair=? ORDER BY minute DESC LIMIT 1 OFFSET ?",
                    (p, keep_per_pair)) as cur:
                row = await cur.fetchone()
                if row:
                    await self.db.execute(
                        "DELETE FROM candles WHERE pair=? AND minute<?", (p, row["minute"]))
        await self.db.commit()

    # ---------------- signals ----------------
    async def insert_signal(self, sig) -> int:
        cur = await self.db.execute(
            "INSERT INTO signals(pair,minute,direction,score,confidence,entry,close,result,factors,created_at,settled_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sig.pair, sig.candle_minute, sig.direction, sig.score, sig.confidence,
             sig.entry, sig.close, sig.result, json.dumps(sig.factors),
             sig.created_at, sig.settled_at))
        await self.db.commit()
        return cur.lastrowid

    async def settle_signal(self, sig_id: int, close: float, result: str, settled_at: float):
        await self.db.execute(
            "UPDATE signals SET close=?, result=?, settled_at=? WHERE id=?",
            (close, result, settled_at, sig_id))
        await self.db.commit()

    async def pending_signals(self) -> List[Dict[str, Any]]:
        async with self.db.execute(
                "SELECT * FROM signals WHERE result IS NULL ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_signal_result(self, sig_id: int, close: float, result: str):
        await self.settle_signal(sig_id, close, result, time.time())

    async def signals_window(self, window_sec: int, pair: Optional[str] = None,
                             direction: Optional[str] = None,
                             result: Optional[str] = None, limit: int = 400) -> List[Dict[str, Any]]:
        since = time.time() - window_sec
        q = "SELECT * FROM signals WHERE created_at>=?"
        args: list = [since]
        if pair:
            q += " AND pair=?"; args.append(pair)
        if direction:
            q += " AND direction=?"; args.append(direction)
        if result:
            q += " AND result=?"; args.append(result)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        async with self.db.execute(q, args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            try:
                r["factors"] = json.loads(r["factors"] or "[]")
            except Exception:
                r["factors"] = []
        return rows

    async def stats(self, window_sec: int) -> Dict[str, Any]:
        since = time.time() - window_sec

        def winrate(w, l):
            t = w + l
            return round(100.0 * w / t, 1) if t else 0.0

        async with self.db.execute(
                "SELECT pair, direction, result, confidence FROM signals WHERE created_at>=? AND result IS NOT NULL",
                (since,)) as cur:
            rows = await cur.fetchall()

        total = {"win": 0, "loss": 0, "tie": 0}
        per_pair: Dict[str, Dict[str, Any]] = {}
        per_dir = {"CALL": {"win": 0, "loss": 0, "tie": 0}, "PUT": {"win": 0, "loss": 0, "tie": 0}}
        per_tier: Dict[str, Dict[str, Any]] = {}

        for r in rows:
            res = r["result"]
            p, d, c = r["pair"], r["direction"], r["confidence"]
            total[res.lower()] = total.get(res.lower(), 0) + 1
            per_dir.setdefault(d, {"win": 0, "loss": 0, "tie": 0})
            per_dir[d][res.lower()] += 1
            pp = per_pair.setdefault(p, {"win": 0, "loss": 0, "tie": 0,
                                         "call_win": 0, "call_loss": 0, "put_win": 0, "put_loss": 0})
            pp[res.lower()] += 1
            if d == "CALL":
                key = "call_" + res.lower()
                if key in pp: pp[key] += 1
            else:
                key = "put_" + res.lower()
                if key in pp: pp[key] += 1
            tier = "strong" if c >= 78 else ("medium" if c >= 63 else "weak")
            pt = per_tier.setdefault(tier, {"win": 0, "loss": 0, "tie": 0})
            pt[res.lower()] += 1

        for p, pp in per_pair.items():
            pp["winrate"] = winrate(pp["win"], pp["loss"])
            pp["call_winrate"] = winrate(pp["call_win"], pp["call_loss"])
            pp["put_winrate"] = winrate(pp["put_win"], pp["put_loss"])
        for t, pt in per_tier.items():
            pt["winrate"] = winrate(pt["win"], pt["loss"])

        return {
            "window_sec": window_sec,
            "total": {**total, "winrate": winrate(total["win"], total["loss"])},
            "per_pair": per_pair,
            "per_direction": {
                "CALL": {**per_dir.get("CALL", {"win": 0, "loss": 0, "tie": 0}),
                         "winrate": winrate(per_dir.get("CALL", {}).get("win", 0), per_dir.get("CALL", {}).get("loss", 0))},
                "PUT": {**per_dir.get("PUT", {"win": 0, "loss": 0, "tie": 0}),
                        "winrate": winrate(per_dir.get("PUT", {}).get("win", 0), per_dir.get("PUT", {}).get("loss", 0))},
            },
            "per_tier": per_tier,
        }


db = DB()
