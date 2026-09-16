"""Candle engine: tick -> M1 candle aggregation with micro-structure stats.

Candle-parity design (verified against REAL captured Quotex payloads):

  * Bucketing uses the SERVER timestamp carried by every tick — the exact
    same minute boundaries the Quotex terminal uses (unix-minute aligned).
  * A smoothed server-clock offset (tick ts vs local receive time) corrects
    every local-clock use (running-minute detection, seconds_left).
  * Late/out-of-order ticks (batch straddling a minute boundary) are routed
    back to their OWN closed candle instead of polluting the running one.
  * history/list/v2 "candles" rows are server snapshots. CRITICAL FINDING
    from live captures: the trailing 1-3 rows are STALE mid-minute
    snapshots (e.g. ticks=8, last_ts=minute+4s while the true minute had
    ~140 ticks). The Quotex terminal itself draws the live edge from the
    tick stream and only uses server rows for older history — so we do the
    same:
        row.last_ts >= minute + 50  -> COMPLETE row: authoritative, refresh
        row.last_ts <  minute + 50  -> STALE snapshot: fills a missing
                                       minute only; NEVER overwrites a
                                       locally live-collected candle
    The running-minute row merges open/widen-H/L; its close is taken only
    when the server row is genuinely fresher than our last received tick.
"""
import time
from typing import Callable, Dict, List, Optional

from ..models import Candle


class CandleEngine:
    """One instance per pair. Feeds ticks, emits running updates and closes."""

    MAX_HISTORY = 400

    def __init__(self, pair: str,
                 on_close: Optional[Callable] = None,
                 on_update: Optional[Callable] = None):
        self.pair = pair
        self.candles: List[Candle] = []
        self.running: Optional[Candle] = None
        self.on_close = on_close      # async (candle, history)
        self.on_update = on_update    # (candle, seconds_left)
        # server-clock tracking: smoothed (server_ts - local_recv_time)
        self.clock_offset = 0.0
        self.last_tick_ts = 0.0       # last SERVER timestamp seen

    # ------------------------------------------------------------ clock
    def server_now(self, now_hint: float = 0.0) -> float:
        """Best estimate of the Quotex server clock 'now'.

        Prefers real server evidence (last tick ts / payload hint) and falls
        back to the offset-corrected local clock."""
        t = time.time() + self.clock_offset
        return max(t, self.last_tick_ts, now_hint)

    def _note_tick_clock(self, ts: float):
        """Track server-clock offset from each received tick."""
        prev = self.last_tick_ts
        self.last_tick_ts = max(self.last_tick_ts, ts)
        off = ts - time.time()
        if not prev:
            self.clock_offset = off
        else:
            # EMA smoothing: single-tick network jitter must not swing minutes
            self.clock_offset = self.clock_offset * 0.9 + off * 0.1

    # ------------------------------------------------------------ feed
    def add_tick(self, price: float, ts: float):
        self._note_tick_clock(ts)
        minute = int(ts // 60) * 60
        sec_into = ts - minute

        if self.running is None:
            self.running = Candle(minute=minute, pair=self.pair)
            self.running.add_tick(price, sec_into, ts)
            if self.on_update:
                self.on_update(self.running, 60 - sec_into)
            return

        if minute > self.running.minute:
            prev = self.running
            self._finalize(prev)
            self.running = Candle(minute=minute, pair=self.pair)
            self.running.add_tick(price, sec_into, ts)
            self._emit_close(prev)
            if self.on_update:
                self.on_update(self.running, 60 - sec_into)
            return

        if minute < self.running.minute:
            # LATE / out-of-order tick (a batch that straddles the minute
            # boundary, or a delayed re-delivery). Route it to its own
            # closed candle — never pollute the fresh running candle.
            self._apply_late_tick(minute, ts, price)
            return

        self.running.add_tick(price, sec_into, ts)
        if self.on_update:
            self.on_update(self.running, 60 - sec_into)

    def _apply_late_tick(self, minute: int, ts: float, price: float):
        target = None
        for c in reversed(self.candles):          # newest-first scan
            if c.minute == minute:
                target = c
                break
            if c.minute < minute:
                break
        if target is None or ts <= target.last_ts:
            return                                 # unknown minute or stale dup
        # extend the closed candle the same way the server would have
        if price > target.high:
            target.high = price
        if price < target.low:
            target.low = price
        if ts > target.last_ts:
            target.close = price
            target.last_ts = ts
        target.ticks += 1

    def _finalize(self, c: Candle):
        c.closed = True
        self.candles.append(c)
        if len(self.candles) > self.MAX_HISTORY:
            self.candles = self.candles[-self.MAX_HISTORY:]

    def _emit_close(self, c: Candle):
        if self.on_close:
            self.on_close(c, list(self.candles))

    # ------------------------------------------------------------- seed
    def seed_history(self, history: List, quiet: bool = True):
        """Bootstrap from tick history [[ts, price, flag], ...] (fast-forward).

        quiet=True (default) suppresses candle-close events during the
        replay so historical minutes never fire signals."""
        saved_close = self.on_close
        if quiet:
            self.on_close = None
        try:
            self.candles = []
            self.running = None
            for ts, price, _flag in history:
                self.add_tick(float(price), float(ts))
            # ensure last candle exists
            if self.running is None:
                now = int(self.server_now() // 60) * 60
                self.running = Candle(minute=now, pair=self.pair)
        finally:
            self.on_close = saved_close

    def seed_server_candles(self, rows: List,
                            now_hint: float = 0.0) -> Dict:
        """Merge server-computed M1 OHLC rows from history/list/v2.

        Row format (newest-first as delivered by Quotex):
            [minute, open, close, high, low, ticks, last_tick_ts]

        Completeness rules (see module docstring — verified on real data):
          * last_ts >= minute+50 -> COMPLETE: authoritative for that minute,
            refreshes any local value (correction recorded when close moves)
          * last_ts <  minute+50 -> STALE mid-minute snapshot: only fills a
            minute we have NO candle for (provisional); a locally
            live-collected candle is always kept (it is fresher/more complete)
          * row for the RUNNING minute: server open beats our partial open,
            H/L widen-only, close only if server row is newer than our tick
          * local candles NEWER than the batch are always kept
        Returns {"changed": n, "corrections": [ {minute, old_close,
        new_close}, ... ]} — corrections drive WIN/LOSS re-settlement.
        """
        now_minute = int(self.server_now(now_hint) // 60) * 60
        incoming = {}
        for r in rows:
            try:
                minute = int(r[0])
                o, c, h, l = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                ticks = int(r[5]) if len(r) > 5 and r[5] else 0
                last_ts = float(r[6]) if len(r) > 6 and r[6] else 0.0
            except (ValueError, TypeError, IndexError):
                continue
            if minute > now_minute:
                continue                      # clock-skew guard: never accept future
            incoming[minute] = (o, h, l, c, ticks, last_ts)
        if not incoming:
            return {"changed": 0, "corrections": []}

        local = {c.minute: c for c in self.candles}
        changed = 0
        corrections: List[Dict] = []
        for minute, (o, h, l, c, ticks, last_ts) in incoming.items():
            if minute == now_minute:
                continue                      # handled below as running candle
            complete = last_ts >= minute + 50
            lc = local.get(minute)
            if lc is not None:
                if complete:
                    if (lc.open, lc.high, lc.low, lc.close) != (o, h, l, c):
                        if lc.close != c:
                            corrections.append({
                                "minute": minute,
                                "old_close": lc.close, "new_close": c,
                            })
                        # authoritative refresh, keep local micro stats
                        lc.open, lc.high, lc.low, lc.close = o, h, l, c
                        lc.ticks = max(lc.ticks, ticks)
                        lc.last_ts = max(lc.last_ts, last_ts)
                        changed += 1
                    elif lc.last_ts < last_ts:
                        lc.last_ts = last_ts
                # else: STALE row + live-collected local candle -> keep local
            else:
                nc = Candle(minute=minute, pair=self.pair,
                            open=o, high=h, low=l, close=c, ticks=ticks)
                nc.closed = True
                nc.last_ts = last_ts
                local[minute] = nc
                changed += 1
        self.candles = [local[m] for m in sorted(local)][-self.MAX_HISTORY:]

        # running candle row (current minute) — server may know more than us
        run_row = incoming.get(now_minute)
        if run_row is not None:
            o, h, l, c, ticks, last_ts = run_row
            if self.running is None or self.running.minute != now_minute:
                self.running = Candle(minute=now_minute, pair=self.pair,
                                      open=o, high=h, low=l, close=c, ticks=ticks)
                self.running.last_ts = last_ts
            else:
                run = self.running
                if run.ticks == 0:
                    # we have no live ticks yet — accept the server row whole
                    run.open, run.high, run.low, run.close = o, h, l, c
                    run.ticks = ticks
                    run.last_ts = last_ts
                else:
                    run.open = o                      # server open beats our partial one
                    run.high = max(run.high, h)       # widen only — never shrink live H/L
                    run.low = min(run.low, l)
                    if last_ts > run.last_ts:
                        # server genuinely fresher than our last tick — take it
                        run.close = c
                        run.last_ts = last_ts
                    run.ticks = max(run.ticks, ticks)
        elif self.running is None:
            self.running = Candle(minute=now_minute, pair=self.pair)
        return {"changed": changed, "corrections": corrections}

    # ------------------------------------------------------------- info
    @property
    def seconds_left(self) -> int:
        if self.running is None:
            return 60
        return max(0, 60 - (self.server_now() - self.running.minute))

    def micro_snapshot(self) -> Dict:
        """Live micro-structure of the running candle (for last-10s meter)."""
        c = self.running
        if c is None:
            return {"seconds_left": 60, "bias": 0.0, "flip_risk": False,
                    "body_pp": 0.0, "dir": 0}
        sec_left = max(0, 60 - (self.server_now() - c.minute))
        bias = c.last10_bias if sec_left <= 10 else c.delta_norm
        open_ = c.open
        # flip risk: tiny body + opposite late momentum
        body_abs = abs(c.close - open_)
        ref = (c.high - c.low) or 1e-12
        flip = (body_abs / ref) < 0.18 and c.ticks > 12 and abs(bias) > 0.5 and sec_left <= 12
        return {
            "seconds_left": int(sec_left),
            "bias": round(bias, 3),
            "flip_risk": bool(flip),
            "body_pp": round((c.close - open_) / max(abs(open_), 1e-12) * 10000, 2),
            "dir": c.direction,
            "ticks": c.ticks,
            "close_pos": round(c.close_pos, 3),
        }

    def snapshot(self) -> Dict:
        c = self.running
        return {
            "pair": self.pair,
            "running": c.to_dict() if c else None,
            "seconds_left": self.seconds_left,
            "micro": self.micro_snapshot(),
        }
