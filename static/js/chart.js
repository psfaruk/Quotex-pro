/* QX chart module — TradingView Lightweight Charts™ (vendored v4.2.3).
   The custom canvas chart was fully removed; rendering now uses the same
   open-source library that powers countless trading platforms:
     * setData()   — full history reload (pair switch / chart open): server
                     candles from Quotex (history/list/v2 "candles" rows),
                     so the chart is 1:1 with the broker terminal.
     * setRunning()— per-tick series.update() of the running candle: the
                     last price line ALWAYS equals the actual Quotex tick
                     (no interpolation/smoothing — 100% price match). The
                     library redraws internally at display frame rate.
     * setMarks()  — signal arrows (CALL up / PUT down, ✓ WIN / ✗ LOSS).
     * setEntry()  — dashed accent price line for the pending signal entry.
     * countdown   — wall-clock seconds to candle close (server-synced). */
(function () {
  'use strict';

  const GREEN = '#00c896', RED = '#ff5b6e', ACCENT = '#ffb020';

  const num = (v) => (v != null && isFinite(v) ? +v : null);

  function toBar(c) {
    if (!c) return null;
    const time = c.minute != null ? c.minute : c.time;
    const bar = {
      time: +time,
      open: num(c.open != null ? c.open : c.o),
      high: num(c.high != null ? c.high : c.h),
      low: num(c.low != null ? c.low : c.l),
      close: num(c.close != null ? c.close : c.c),
    };
    if (bar.open == null || bar.high == null || bar.low == null ||
        bar.close == null || bar.time == null) return null;
    return bar;
  }

  class QXChart {
    constructor(container) {
      if (!window.LightweightCharts) {
        throw new Error('LightweightCharts library not loaded');
      }
      this.container = container;
      this._crosshair = false;
      this._running = null;
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
      this.chart.subscribeCrosshairMove((param) => {
        this._crosshair = !!(param && param.time != null);
        if (param && param.seriesData && param.seriesData.get(this.series)) {
          const b = param.seriesData.get(this.series);
          this._emitOhlc({ open: b.open, high: b.high, low: b.low, close: b.close });
        } else if (!this._crosshair) {
          this._emitOhlc(this._running);
        }
      });

      // keep the chart perfectly sized with its container (tab switches too)
      this._ro = new ResizeObserver(() => this._resize());
      this._ro.observe(container);
      this._resize();
    }

    _resize() {
      const w = this.container.clientWidth || 0;
      const h = this.container.clientHeight || 0;
      if (w > 0 && h > 0) this.chart.applyOptions({ width: w, height: h });
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
      this.series.applyOptions({
        priceFormat: { type: 'price', precision: dec, minMove },
      });
    }

    // ------------------------------------------------------------ public API
    setData(candles, running, secondsLeft) {
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
      this._running = r ? {
        minute: r.time, open: r.open, high: r.high, low: r.low, close: r.close,
      } : null;
      this._applyPrecision(r ? r.close : (bars.length ? bars[bars.length - 1].close : null));
      this.series.setData(bars);
      this.series.setMarkers(this._markers);
      if (secondsLeft != null && isFinite(secondsLeft)) this._syncSl(secondsLeft);
      if (!this._crosshair) this._emitOhlc(this._running);
    }

    setRunning(running, secondsLeft) {
      const b = toBar(running);
      if (!b) return;
      this.series.update(b);                        // replace-or-append by time
      this._running = {
        minute: b.time, open: b.open, high: b.high, low: b.low, close: b.close,
      };
      if (this._lastPrecisionPrice == null ||
          Math.abs(this._lastPrecisionPrice - b.close) > Math.abs(b.close) * 0.5) {
        this._applyPrecision(b.close);
      }
      this._lastPrecisionPrice = b.close;
      if (secondsLeft != null && isFinite(secondsLeft)) this._syncSl(secondsLeft);
      if (!this._crosshair) this._emitOhlc(this._running);
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
      this.series.setMarkers(this._markers);
    }

    setEntry(price) {
      if (this._entryLine) {
        this.series.removePriceLine(this._entryLine);
        this._entryLine = null;
      }
      if (price != null && isFinite(price)) {
        this._entryLine = this.series.createPriceLine({
          price: +price,
          color: ACCENT,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: 'ENTRY',
        });
      }
    }

    resetView() {
      this.chart.timeScale().resetTimeScale();
      this.chart.timeScale().fitContent();
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
