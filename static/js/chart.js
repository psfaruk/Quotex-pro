/* Custom canvas candlestick chart — no external deps.
   Rendering: 60fps requestAnimationFrame loop; the running candle is
   interpolated between ticks (exponential smoothing) so it glides like a
   video instead of jumping once per server update. Countdown is computed
   from wall-clock time synced against the server's seconds_left.
   Layout: candle slots are width-capped and RIGHT-ALIGNED, so a short
   history (e.g. right after a pair switch) renders as normal-width
   candles hugging the right edge — never giant stretched blocks.
   Features: live running candle, entry line, signal arrows, wheel zoom,
   drag pan, crosshair OHLC, price scale. */
(function () {
  'use strict';

  const GREEN = '#00c896', RED = '#ff5b6e', DIM = '#5a6785',
        ACCENT = '#ffb020', TXT = '#dbe4f3';
  const MAX_CW = 13;    // max slot width per candle (px) when history is long
  const MAX_CW_SHORT = 26;  // wider slots allowed for short histories
  const SMOOTH = 11;    // interpolation speed (per second) toward latest tick

  class CandleChart {
    constructor(canvas) {
      this.cv = canvas;
      this.ctx = canvas.getContext('2d');
      this.candles = [];          // closed candles {minute,o,h,l,c}
      this.running = null;        // latest server snapshot of running candle
      this._anim = null;          // interpolated running candle (rendered)
      this.entry = null;          // entry price line
      this.signalMarks = [];      // {minute, dir, result}
      this.visible = 60;          // candles visible
      this.offset = 0;            // candles panned from right
      this.hover = null;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      // countdown sync: server seconds_left captured at receive time
      this._slServer = 60;
      this._slRecv = performance.now();
      this._lastT = performance.now();
      this._rafOn = false;
      this.onOhlc = null;
      this._bind();
      this.resize();
      this.start();
    }

    _bind() {
      const cv = this.cv;
      window.addEventListener('resize', () => this.resize());
      cv.addEventListener('wheel', (e) => {
        e.preventDefault();
        const old = this.visible;
        this.visible = Math.max(20, Math.min(200, Math.round(this.visible * (e.deltaY > 0 ? 1.12 : 0.89))));
      }, { passive: false });
      cv.addEventListener('pointerdown', (e) => {
        this._drag = { x: e.clientX, offset: this.offset };
        cv.setPointerCapture(e.pointerId);
      });
      cv.addEventListener('pointermove', (e) => {
        const r = this._layout();
        if (r) this.hover = { x: e.clientX - r.rect.left };
        if (this._drag) {
          const dx = e.clientX - this._drag.x;
          const cw = r ? r.cw : 10;
          this.offset = Math.max(0, Math.min(this.candles.length, this._drag.offset + Math.round(dx / cw)));
        }
      });
      cv.addEventListener('pointerup', () => { this._drag = null; });
      cv.addEventListener('pointerleave', () => { this.hover = null; });
    }

    resize() {
      const rect = this.cv.getBoundingClientRect();
      this.cv.width = Math.max(1, Math.round(rect.width * this.dpr));
      this.cv.height = Math.max(1, Math.round(rect.height * this.dpr));
      this.w = rect.width; this.h = rect.height;
    }

    // ------------------------------------------------------------ rAF loop
    start() {
      if (this._rafOn) return;
      this._rafOn = true;
      const loop = (t) => {
        if (!this._rafOn) return;
        this._frame(t);
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    }

    stop() { this._rafOn = false; }

    _frame(t) {
      const dt = Math.min(0.1, (t - this._lastT) / 1000);
      this._lastT = t;
      // idle when the chart is not on screen — costs nothing
      if (this.cv.offsetParent === null || !this.w || this.w < 50) return;
      // interpolate the running candle toward the latest server snapshot
      if (this.running) {
        if (!this._anim || this._anim.minute !== this.running.minute) {
          this._anim = Object.assign({}, this.running);   // new candle: snap
        } else {
          const a = 1 - Math.exp(-dt * SMOOTH);
          this._anim.c += (this.running.c - this._anim.c) * a;
          this._anim.h += (this.running.h - this._anim.h) * a;
          this._anim.l += (this.running.l - this._anim.l) * a;
          this._anim.o = this.running.o;
        }
      } else {
        this._anim = null;
      }
      this.draw();
    }

    // ------------------------------------------------------------ public API
    setData(candles, running, secondsLeft, entry) {
      const norm = (c) => c && {
        minute: c.minute,
        o: c.o != null ? c.o : c.open,
        h: c.h != null ? c.h : c.high,
        l: c.l != null ? c.l : c.low,
        c: c.c != null ? c.c : c.close,
      };
      this.candles = (candles || []).map(norm);
      this.running = running ? norm(running) : null;
      if (!this.running) this._anim = null;
      if (secondsLeft != null && isFinite(secondsLeft)) this._syncSl(secondsLeft);
      if (entry !== undefined) this.entry = entry;
    }

    setRunning(running, secondsLeft) {
      if (!running) return;
      const r = {
        minute: running.minute,
        o: running.o != null ? running.o : running.open,
        h: running.h != null ? running.h : running.high,
        l: running.l != null ? running.l : running.low,
        c: running.c != null ? running.c : running.close,
      };
      this.running = r;
      this._syncSl(secondsLeft);
    }

    resetView() { this.offset = 0; this.visible = 60; this.hover = null; }

    setMarks(marks) { this.signalMarks = marks || []; }

    _syncSl(sl) {
      if (sl == null || !isFinite(sl)) return;
      this._slServer = Math.max(0, sl);
      this._slRecv = performance.now();
    }

    getSecondsLeft() {
      return Math.max(0, this._slServer - (performance.now() - this._slRecv) / 1000);
    }

    // ------------------------------------------------------------ layout
    _layout() {
      if (!this.w) return null;
      const rect = this.cv.getBoundingClientRect();
      const padR = 62, padT = 12, padB = 22, padL = 8;
      const plotW = this.w - padR - padL;
      if (plotW < 50) return null;
      const showRun = (this._anim || this.running) && this.offset === 0;
      const n = Math.min(this.visible, this.candles.length + (showRun ? 1 : 0)) || 1;
      const cap = n < 25 ? MAX_CW_SHORT : MAX_CW;
      const cw = Math.min(plotW / n, cap);
      return { rect, padL, padR, padT, padB, plotW, cw, n, right: this.w - padR };
    }

    // x of candle i (0-based among the n visible slots; last = right edge)
    _xOf(L, i, n) { return L.right - (n - 1 - i) * L.cw - L.cw / 2; }

    _scale(L) {
      const slice = this._visibleCandles();
      let hi = -Infinity, lo = Infinity;
      for (const c of slice) { hi = Math.max(hi, c.h); lo = Math.min(lo, c.l); }
      if (this.offset === 0 && this._anim) {
        hi = Math.max(hi, this._anim.h); lo = Math.min(lo, this._anim.l);
      }
      if (this.entry != null) { hi = Math.max(hi, this.entry); lo = Math.min(lo, this.entry); }
      if (!isFinite(hi) || !isFinite(lo)) { hi = 1; lo = 0; }
      const mid = (hi + lo) / 2, span = Math.max((hi - lo) * 1.18, 1e-9);
      hi = mid + span / 2; lo = mid - span / 2;
      const plotH = this.h - L.padT - L.padB;
      return { hi, lo, y: (p) => L.padT + (hi - p) / (hi - lo) * plotH };
    }

    _visibleCandles() {
      const total = this.candles.length;
      const start = Math.max(0, total - this.visible - this.offset);
      const end = Math.max(start, total - this.offset);
      return this.candles.slice(start, end);
    }

    // ------------------------------------------------------------ draw
    draw() {
      const ctx = this.ctx, L = this._layout();
      if (!L) return;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      const S = this._scale(L);
      const slice = this._visibleCandles();
      const showRun = this._anim && this.offset === 0;
      const n = slice.length + (showRun ? 1 : 0);

      // grid + price scale
      ctx.font = '10px Inter, sans-serif';
      ctx.textAlign = 'left';
      const rows = 6;
      for (let i = 0; i <= rows; i++) {
        const p = S.lo + (S.hi - S.lo) * i / rows;
        const y = S.y(p);
        ctx.strokeStyle = 'rgba(35,46,71,.55)';
        ctx.beginPath(); ctx.moveTo(L.padL, y); ctx.lineTo(L.right, y); ctx.stroke();
        ctx.fillStyle = DIM;
        ctx.fillText(this._fmt(p), L.right + 6, y + 3.5);
      }

      // candles
      const bw = Math.max(2.5, L.cw * 0.62);
      const drawCandle = (c, x, running) => {
        const up = c.c >= c.o;
        const col = up ? GREEN : RED;
        const yo = S.y(c.o), yc = S.y(c.c), yh = S.y(c.h), yl = S.y(c.l);
        ctx.strokeStyle = col; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, yh); ctx.lineTo(x, yl); ctx.stroke();
        const top = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
        ctx.fillStyle = col;
        if (running) {
          // pulsing running candle: glowing body + crisp outline
          ctx.globalAlpha = 0.3 + 0.3 * Math.abs(Math.sin(Date.now() / 400));
          ctx.fillRect(x - bw / 2, top, bw, hgt);
          ctx.globalAlpha = 1;
          ctx.strokeStyle = col;
          ctx.strokeRect(x - bw / 2, top, bw, hgt);
        } else {
          ctx.fillRect(x - bw / 2, top, bw, hgt);
        }
      };

      slice.forEach((c, i) => drawCandle(c, this._xOf(L, i, n), false));
      if (showRun) drawCandle(this._anim, this._xOf(L, n - 1, n), true);

      // hint in the empty left area when history is too short to fill the plot
      if (n * L.cw < L.plotW - 70) {
        ctx.fillStyle = 'rgba(90,103,133,.7)';
        ctx.font = '11px Inter, sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText('◀ পুরনো হিস্ট্রি এখনো কম — লাইভ ক্যান্ডেল জমতে থাকবে', L.padL + 10, L.padT + 26);
      }

      // open (entry) price dashed line for the running candle
      if (this.entry != null) {
        const y = S.y(this.entry);
        ctx.setLineDash([4, 4]); ctx.strokeStyle = ACCENT; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(L.padL, y); ctx.lineTo(L.right, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = ACCENT;
        ctx.font = 'bold 9.5px Inter, sans-serif';
        ctx.fillText('ENTRY', L.right + 6, y - 4);
      }

      // signal arrows on historical candles
      const minDir = {};
      for (const m of this.signalMarks) minDir[m.minute] = m;
      ctx.font = 'bold 11px Inter, sans-serif'; ctx.textAlign = 'center';
      slice.forEach((c, i) => {
        const m = minDir[c.minute];
        if (!m) return;
        const x = this._xOf(L, i, n);
        const isWin = m.result === 'WIN', isLoss = m.result === 'LOSS';
        ctx.fillStyle = isWin ? GREEN : (isLoss ? RED : ACCENT);
        if (m.dir === 'CALL') {
          const y = S.y(c.l) + 12;
          ctx.fillText('▲', x, y);
          if (m.result) ctx.fillText(isWin ? '✓' : (isLoss ? '✗' : '•'), x, y + 11);
        } else {
          const y = S.y(c.h) - 4;
          ctx.fillText('▼', x, y);
          if (m.result) ctx.fillText(isWin ? '✓' : (isLoss ? '✗' : '•'), x, y - 11);
        }
      });
      ctx.textAlign = 'left';

      // crosshair + hover OHLC
      let hovered = null;
      if (this.hover && this.hover.x > L.right - n * L.cw && this.hover.x < L.right) {
        const k = Math.floor((L.right - this.hover.x) / L.cw);   // slots to the right
        const idx = n - 1 - k;
        const combined = showRun ? slice.concat([this._anim]) : slice;
        hovered = combined[idx] || null;
        if (hovered) {
          ctx.setLineDash([3, 3]); ctx.strokeStyle = 'rgba(219,228,243,.25)';
          ctx.beginPath(); ctx.moveTo(this.hover.x, L.padT); ctx.lineTo(this.hover.x, this.h - L.padB); ctx.stroke();
          ctx.setLineDash([]);
        }
      }

      // running OHLC readout (or hovered candle when crosshair active)
      const last = hovered || (showRun ? this._anim : slice[slice.length - 1]);
      if (last) {
        const label = `O ${this._fmt(last.o)}  H ${this._fmt(last.h)}  L ${this._fmt(last.l)}  C ${this._fmt(last.c)}`;
        this._emitOhlc(label, last);
      }

      // last price line + right badge
      if (last) {
        const y = S.y(last.c);
        const up = last.c >= last.o;
        ctx.strokeStyle = up ? GREEN : RED;
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(L.padL, y); ctx.lineTo(L.right, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = up ? GREEN : RED;
        ctx.fillRect(L.right + 2, y - 8, 56, 16);
        ctx.fillStyle = '#04121a';
        ctx.font = 'bold 10px Inter, monospace';
        ctx.fillText(this._fmt(last.c), L.right + 7, y + 3.5);
      }
    }

    _fmt(p) {
      if (p >= 100) return p.toFixed(2);
      if (p >= 10) return p.toFixed(3);
      return p.toFixed(5);
    }

    _emitOhlc(label, c) {
      if (this.onOhlc) this.onOhlc(label, c);
    }
  }

  window.CandleChart = CandleChart;
})();
