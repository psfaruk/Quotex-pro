/* Custom canvas candlestick chart — no external deps.
   Features: live running candle, entry line, signal arrows, wheel zoom,
   drag pan, crosshair OHLC, price scale, countdown sync. */
(function () {
  'use strict';

  const GREEN = '#00c896', RED = '#ff5b6e', DIM = '#5a6785', LINE = '#232e47',
        ACCENT = '#ffb020', TXT = '#dbe4f3';

  class CandleChart {
    constructor(canvas) {
      this.cv = canvas;
      this.ctx = canvas.getContext('2d');
      this.candles = [];          // closed candles {minute,o,h,l,c}
      this.running = null;        // {minute,o,h,l,c}
      this.secondsLeft = 60;
      this.entry = null;          // entry price line
      this.signalMarks = [];      // {minute, dir, result}
      this.visible = 60;          // candles visible
      this.offset = 0;            // candles panned from right
      this.hover = null;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      this._bind();
      this.resize();
    }

    _bind() {
      const cv = this.cv;
      window.addEventListener('resize', () => this.resize());
      cv.addEventListener('wheel', (e) => {
        e.preventDefault();
        const old = this.visible;
        this.visible = Math.max(20, Math.min(200, Math.round(this.visible * (e.deltaY > 0 ? 1.12 : 0.89))));
        if (this.visible !== old) this.draw();
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
          this.draw();
        }
      });
      cv.addEventListener('pointerup', () => { this._drag = null; });
      cv.addEventListener('pointerleave', () => { this.hover = null; this.draw(); });
    }

    resize() {
      const rect = this.cv.getBoundingClientRect();
      this.cv.width = rect.width * this.dpr;
      this.cv.height = rect.height * this.dpr;
      this.w = rect.width; this.h = rect.height;
      this.draw();
    }

    setData(candles, running, secondsLeft, entry) {
      // normalize candle format: accept {open,high,low,close} or {o,h,l,c}
      const norm = (c) => c && {
        minute: c.minute,
        o: c.o != null ? c.o : c.open,
        h: c.h != null ? c.h : c.high,
        l: c.l != null ? c.l : c.low,
        c: c.c != null ? c.c : c.close,
      };
      this.candles = (candles || []).map(norm);
      this.running = running ? norm(running) : null;
      this.secondsLeft = secondsLeft;
      if (entry !== undefined) this.entry = entry;
      this.draw();
    }

    setMarks(marks) { this.signalMarks = marks || []; }

    _layout() {
      if (!this.w) return null;
      const padR = 62, padT = 12, padB = 22, padL = 8;
      const plotW = this.w - padR - padL;
      if (plotW < 50) return null;
      const n = Math.min(this.visible, this.candles.length + (this.running ? 1 : 0)) || 1;
      const cw = plotW / n;
      return { padL, padR, padT, padB, plotW, cw, n };
    }

    _scale(L) {
      const slice = this._visibleCandles();
      let hi = -Infinity, lo = Infinity;
      for (const c of slice) { hi = Math.max(hi, c.h); lo = Math.min(lo, c.l); }
      if (this.running) { hi = Math.max(hi, this.running.h); lo = Math.min(lo, this.running.l); }
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

    draw() {
      const ctx = this.ctx, L = this._layout();
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      if (!L) return;
      const S = this._scale(L);
      const slice = this._visibleCandles();
      const total = this.candles.length;
      const startIdx = Math.max(0, total - this.visible - this.offset);

      // grid + price scale
      ctx.font = '10px Inter, sans-serif';
      ctx.textAlign = 'left';
      const rows = 6;
      for (let i = 0; i <= rows; i++) {
        const p = S.lo + (S.hi - S.lo) * i / rows;
        const y = S.y(p);
        ctx.strokeStyle = 'rgba(35,46,71,.55)';
        ctx.beginPath(); ctx.moveTo(L.padL, y); ctx.lineTo(this.w - L.padR, y); ctx.stroke();
        ctx.fillStyle = DIM;
        ctx.fillText(this._fmt(p), this.w - L.padR + 6, y + 3.5);
      }

      // candles
      const bw = Math.max(2.5, L.cw * 0.62);
      const drawCandle = (c, x, running) => {
        const up = c.c >= c.o;
        const col = running ? (up ? GREEN : RED) : (up ? GREEN : RED);
        const yo = S.y(c.o), yc = S.y(c.c), yh = S.y(c.h), yl = S.y(c.l);
        ctx.strokeStyle = col; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, yh); ctx.lineTo(x, yl); ctx.stroke();
        const top = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
        ctx.fillStyle = col;
        if (running) {
          // pulsing running candle: hollow body + glow wick
          ctx.globalAlpha = 0.25 + 0.35 * Math.abs(Math.sin(Date.now() / 400));
          ctx.fillRect(x - bw / 2, top, bw, hgt);
          ctx.globalAlpha = 1;
          ctx.strokeStyle = col;
          ctx.strokeRect(x - bw / 2, top, bw, hgt);
        } else {
          ctx.fillRect(x - bw / 2, top, bw, hgt);
        }
      };

      slice.forEach((c, i) => drawCandle(c, L.padL + i * L.cw + L.cw / 2, false));
      if (this.running) drawCandle(this.running, L.padL + slice.length * L.cw + L.cw / 2, true);

      // open (entry) price dashed line for the running candle
      if (this.entry != null) {
        const y = S.y(this.entry);
        ctx.setLineDash([4, 4]); ctx.strokeStyle = ACCENT; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(L.padL, y); ctx.lineTo(this.w - L.padR, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = ACCENT;
        ctx.font = 'bold 9.5px Inter, sans-serif';
        ctx.fillText('ENTRY', this.w - L.padR + 6, y - 4);
      }

      // signal arrows on historical candles
      const minDir = {};
      for (const m of this.signalMarks) minDir[m.minute] = m;
      ctx.font = 'bold 11px Inter, sans-serif'; ctx.textAlign = 'center';
      slice.forEach((c, i) => {
        const m = minDir[c.minute];
        if (!m) return;
        const x = L.padL + i * L.cw + L.cw / 2;
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

      // crosshair
      if (this.hover && this.hover.x > L.padL && this.hover.x < this.w - L.padR) {
        const idx = Math.floor((this.hover.x - L.padL) / L.cw);
        const c = slice[idx] || (idx === slice.length ? this.running : null);
        if (c) {
          ctx.setLineDash([3, 3]); ctx.strokeStyle = 'rgba(219,228,243,.25)';
          ctx.beginPath(); ctx.moveTo(this.hover.x, L.padT); ctx.lineTo(this.hover.x, this.h - L.padB); ctx.stroke();
          ctx.setLineDash([]);
          const label = `O ${this._fmt(c.o)}  H ${this._fmt(c.h)}  L ${this._fmt(c.l)}  C ${this._fmt(c.c)}`;
          this._emitOhlc(label, c);
        }
      }

      // last price line
      const last = this.running || slice[slice.length - 1];
      if (last) {
        const y = S.y(last.c);
        const up = last.c >= last.o;
        ctx.strokeStyle = up ? GREEN : RED;
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(L.padL, y); ctx.lineTo(this.w - L.padR, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = up ? GREEN : RED;
        ctx.fillRect(this.w - L.padR + 2, y - 8, 56, 16);
        ctx.fillStyle = '#04121a';
        ctx.font = 'bold 10px Inter, monospace';
        ctx.fillText(this._fmt(last.c), this.w - L.padR + 7, y + 3.5);
      }
    }

    _fmt(p) {
      if (p >= 100) return p.toFixed(2);
      if (p >= 10) return p.toFixed(3);
      return p.toFixed(5);
    }

    _emitOhlc(label, c) {
      if (this.onOhlc) this.onOhlc(label);
    }
  }

  window.CandleChart = CandleChart;
})();
