/* QX chart module — TradingView Lightweight Charts™ (vendored v4.2.3).
   The custom canvas chart was fully removed; rendering now uses the same
   open-source library that powers countless trading platforms:
     * setData(pair,...)   — full history reload (pair switch / chart open):
                     server candles from Quotex (history/list/v2 "candles"
                     rows), so the chart is 1:1 with the broker terminal.
                     Ignored unless pair === current pair (no cross-pair race).
     * setRunning(pair,...)— per-tick series.update() of the running candle:
                     the last price line ALWAYS equals the actual Quotex tick
                     (no interpolation of resting values — 100% price match).
     * glide animation    — between two ticks of the SAME candle the bar
                     visually glides (ease-out, ~130ms, requestAnimationFrame
                     @ display frame rate) so motion looks 60fps-smooth. The
                     moment a tick lands, and whenever it rests, the bar is
                     the exact tick value again.
     * setMarks()  — signal arrows (CALL up / PUT down, ✓ WIN / ✗ LOSS).
     * setEntry()  — dashed accent price line for the pending signal entry.
     * countdown   — wall-clock seconds to candle close (server-synced).
   Robustness: every library call is guarded — one malformed bar can never
   throw out of the websocket handler and freeze the chart. */
(function () {
  'use strict';

  const GREEN = '#00c896', RED = '#ff5b6e', ACCENT = '#ffb020';
  const GLIDE_MS = 130;          // visual glide between ticks (same candle)

  const num = (v) => (v != null && isFinite(v) ? +v : null);

  function toBar(c) {
    if (!c) return null;
    const time = c.minute != null ? c.minute : c.time;
    let open = num(c.open != null ? c.open : c.o);
    let high = num(c.high != null ? c.high : c.h);
    let low = num(c.low != null ? c.low : c.l);
    let close = num(c.close != null ? c.close : c.c);
    const t = +time;
    if (open == null || high == null || low == null || close == null ||
        !isFinite(t) || t <= 0) return null;
    // sanitize: clamp H/L around O/C (guards against placeholder candles
    // with high=-1e18 / low=1e18 that would break the library autoscale)
    high = Math.max(high, open, close);
    low = Math.min(low, open, close);
    if (high === low) { high += 1e-9; low -= 1e-9; }   // zero-range guard
    return { time: t, open, high, low, close };
  }

  const lerp = (a, b, p) => a + (b - a) * p;
  const easeOut = (p) => 1 - Math.pow(1 - p, 3);

  class QXChart {
    constructor(container) {
      if (!window.LightweightCharts) {
        throw new Error('LightweightCharts library not loaded');
      }
      this.container = container;
      this._crosshair = false;
      this._pair = null;          // pair this chart currently displays
      this._running = null;       // last REAL tick bar (exact Quotex values)
      this._disp = null;          // currently DISPLAYED last bar (may glide)
      this._anim = null;          // {from, to, t0} glide state
      this._bars = [];
      this._markers = [];
      this._entryLine = null;
      this._slServer = 60;
      this._slRecv = performance.now();
      this.onOhlc = null;

      this.chart = LightweightCharts.createChart(container, {
        layout: {
          background: { type: 'solid', color: '#0d1424' },
          textColor: '#8b98b8',
          fontFamily: "Inter, 'Hind Siliguri', sans-serif",
        },
        grid: {
          vertLines: { color: 'rgba(35,46,71,.55)' },
          horzLines: { color: 'rgba(35,46,71,.55)' },
        },
        crosshair: {
          mode: LightweightCharts.CrosshairMode.Normal,
          vertLine: { color: 'rgba(219,228,243,.3)', labelBackgroundColor: '#1d2942' },
          horzLine: { color: 'rgba(219,228,243,.3)', labelBackgroundColor: '#1d2942' },
        },
        rightPriceScale: { borderColor: '#1d2942' },
        timeScale: {
          borderColor: '#1d2942',
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 3,
          barSpacing: 7,
          minBarSpacing: 1.5,
        },
        handleScroll: { mouseWheel: true, pressedMouseMove: true },
        handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
      });

      this.series = this.chart.addCandlestickSeries({
        upColor: GREEN, downColor: RED,
        borderUpColor: GREEN, borderDownColor: RED,
        wickUpColor: GREEN, wickDownColor: RED,
        priceFormat: { type: 'price', precision: 5, minMove: 0.00001 },
      });

      // OHLC legend: hovering a candle shows that candle; otherwise the running one
      try {
        this.chart.subscribeCrosshairMove((param) => {
          this._crosshair = !!(param && param.time != null);
          if (param && param.seriesData && param.seriesData.get(this.series)) {
            const b = param.seriesData.get(this.series);
            this._emitOhlc({ open: b.open, high: b.high, low: b.low, close: b.close });
          } else if (!this._crosshair) {
            this._emitOhlc(this._running);
          }
        });
      } catch (e) { /* crosshair optional */ }

      // keep the chart perfectly sized with its container (tab switches too)
      this._ro = new ResizeObserver(() => this._resize());
      this._ro.observe(container);
      this._resize();

      // 60fps render loop: only does work while a glide animation is active
      this._raf = this._frame.bind(this);
      requestAnimationFrame(this._raf);
    }

    _safe(fn) {
      try { return fn(); } catch (e) { console.warn('[chart]', e && e.message); return undefined; }
    }

    _resize() {
      const w = this.container.clientWidth || 0;
      const h = this.container.clientHeight || 0;
      if (w > 0 && h > 0) this._safe(() => this.chart.applyOptions({ width: w, height: h }));
    }

    _emitOhlc(c) {
      if (!c || !this.onOhlc) return;
      const f = (v) => this._fmt(v);
      this.onOhlc(`O ${f(c.open)}  H ${f(c.high)}  L ${f(c.low)}  C ${f(c.close)}`);
    }

    _fmt(p) {
      if (p == null || !isFinite(p)) return '—';
      const ref = this._running ? this._running.close : p;
      const dec = (ref != null && ref >= 20) ? 3 : 5;
      return (+p).toFixed(dec);
    }

    _applyPrecision(price) {
      // JPY-style pairs (price >= 20) quote with 3 decimals, others with 5 —
      // exactly how the Quotex terminal formats them
      if (price == null || !isFinite(price)) return;
      const dec = price >= 20 ? 3 : 5;
      const minMove = dec === 3 ? 0.001 : 0.00001;
      this._safe(() => this.series.applyOptions({
        priceFormat: { type: 'price', precision: dec, minMove },
      }));
    }

    // ------------------------------------------------------------ pair mgmt
    setPair(pair) {
      if (this._pair === pair) return;
      this._pair = pair;
      this.clear();
    }

    getPair() { return this._pair; }

    clear() {
      // wipe everything so a pair switch starts from a clean slate — no
      // stale bars from the previous pair can race the incoming history
      this._anim = null;
      this._running = null;
      this._disp = null;
      this._bars = [];
      if (this._entryLine) {
        this._safe(() => this.series.removePriceLine(this._entryLine));
        this._entryLine = null;
      }
      this._safe(() => this.series.setMarkers([]));
      this._safe(() => this.series.setData([]));
      this._emitOhlc(null);
    }

    // ------------------------------------------------------------ public API
    setData(pair, candles, running, secondsLeft) {
      if (pair != null) {
        if (this._pair != null && pair !== this._pair) return;  // cross-pair race
        this._pair = pair;                                     // adopt if unset
      }
      const bars = [];
      const seen = new Map();                       // dedupe by time — last wins
      for (const c of (candles || [])) {
        const b = toBar(c);
        if (b) seen.set(b.time, b);
      }
      const r = toBar(running);
      if (r) seen.set(r.time, r);                   // running candle wins over closed dup
      seen.forEach((b) => bars.push(b));
      bars.sort((a, b) => a.time - b.time);
      this._bars = bars;
      this._anim = null;                            // full reload cancels gliding
      this._running = r ? {
        time: r.time, open: r.open, high: r.high, low: r.low, close: r.close,
      } : (bars.length ? { ...bars[bars.length - 1] } : null);
      this._disp = this._running ? { ...this._running } : null;
      this._applyPrecision(r ? r.close : (bars.length ? bars[bars.length - 1].close : null));
      this._safe(() => this.series.setData(bars));
      this._safe(() => this.series.setMarkers(this._markers));
      if (secondsLeft != null && isFinite(secondsLeft)) this._syncSl(secondsLeft);
      if (!this._crosshair) this._emitOhlc(this._running);
    }

    setRunning(pair, candle, secondsLeft) {
      if (pair != null) {
        if (this._pair == null) this._pair = pair;             // adopt first pair
        else if (pair !== this._pair) return;                  // cross-pair race guard
      }
      const b = toBar(candle);
      if (!b) return;
      if (this._running && b.time < this._running.time) return; // never move backwards
      const freshCandle = !(this._running && this._running.time === b.time);
      this._running = { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close };
      if (freshCandle || !this._disp || this._disp.time !== b.time) {
        // new minute (or after full reload): snap, no glide across candles
        this._disp = { ...this._running };
        this._safe(() => this.series.update(this._disp));
      } else if (this._disp) {
        // same candle, new tick: glide from what's on screen to the new tick
        this._anim = {
          from: { ...this._disp },
          to: { ...this._running },
          t0: performance.now(),
        };
      }
      if (this._lastPrecisionPrice == null ||
          Math.abs(this._lastPrecisionPrice - b.close) > Math.abs(b.close) * 0.5) {
        this._applyPrecision(b.close);
      }
      this._lastPrecisionPrice = b.close;
      if (secondsLeft != null && isFinite(secondsLeft)) this._syncSl(secondsLeft);
      if (!this._crosshair) this._emitOhlc(this._running);   // legend = REAL tick
    }

    // ------------------------------------------------------------ 60fps loop
    _frame(now) {
      const anim = this._anim;
      if (anim) {
        const p = Math.min(1, (now - anim.t0) / GLIDE_MS);
        if (p >= 1) {
          this._disp = { ...anim.to };
          this._anim = null;
          this._safe(() => this.series.update(this._disp));  // exact tick at rest
        } else {
          const e = easeOut(p);
          const d = {
            time: anim.to.time,
            open: anim.to.open,
            high: lerp(anim.from.high, anim.to.high, e),
            low: lerp(anim.from.low, anim.to.low, e),
            close: lerp(anim.from.close, anim.to.close, e),
          };
          this._disp = d;
          this._safe(() => this.series.update(d));
        }
      }
      requestAnimationFrame(this._raf);
    }

    setMarks(marks) {
      this._markers = (marks || []).map((m) => ({
        time: +m.minute,
        position: m.dir === 'CALL' ? 'belowBar' : 'aboveBar',
        color: m.result === 'WIN' ? GREEN : (m.result === 'LOSS' ? RED : ACCENT),
        shape: m.dir === 'CALL' ? 'arrowUp' : 'arrowDown',
        text: m.result === 'WIN' ? '✓' : (m.result === 'LOSS' ? '✗' : ''),
      })).filter((m) => isFinite(m.time))
        .sort((a, b) => a.time - b.time);
      this._safe(() => this.series.setMarkers(this._markers));
    }

    setEntry(price) {
      if (this._entryLine) {
        this._safe(() => this.series.removePriceLine(this._entryLine));
        this._entryLine = null;
      }
      if (price != null && isFinite(price)) {
        this._entryLine = this._safe(() => this.series.createPriceLine({
          price: +price,
          color: ACCENT,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: 'ENTRY',
        }));
      }
    }

    resetView() {
      this._safe(() => this.chart.timeScale().resetTimeScale());
      this._safe(() => this.chart.timeScale().fitContent());
    }

    // ---------------------------------------------------------- countdown
    _syncSl(sl) {
      if (sl == null || !isFinite(sl)) return;
      this._slServer = Math.max(0, sl);
      this._slRecv = performance.now();
    }

    getSecondsLeft() {
      return Math.max(0, this._slServer - (performance.now() - this._slRecv) / 1000);
    }
  }

  window.QXChart = QXChart;
})();
