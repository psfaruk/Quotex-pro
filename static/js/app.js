/* App controller: websocket client, state, tab routing, live updates */
(function () {
  'use strict';

  const { $, esc, fmtPrice, fmtTime } = Views.utils;

  const state = {
    status: {},          // last status msg
    settingsMasked: {},
    known_pairs: [],
    activePair: null,
    candles: {},         // pair -> closed candles array
    mini: {},            // pair -> recent candle directions (sparkline)
    running: {},         // pair -> running candle
    secondsLeft: 60,
    micro: {},
    feedItems: [],       // recent signals/results
    stats: {},
    _tokenEdited: false,
    _tab: 'home',
  };

  let chart = null;
  let ws = null;
  let wsRetry = 1000;

  // ------------------------------------------------------------ websocket
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    ws.onopen = () => { wsRetry = 1000; };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      handle(m);
    };
    ws.onclose = () => {
      setTimeout(connectWS, wsRetry);
      wsRetry = Math.min(wsRetry * 1.6, 8000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  function handle(m) {
    switch (m.type) {
      case 'status': {
        state.status = m;
        // mini candle history for home cards
        if (!state.activePair && m.pairs && m.pairs.length) state.activePair = m.pairs[0].pair;
        updateConnBadge();
        Views.renderPairTabs(state);
        Views.renderHome(state);
        if (state._tab === 'settings') Views.renderSettings(state);
        break;
      }
      case 'candle_update': {
        state.running[m.pair] = m.candle;
        state.micro[m.pair] = m.micro || {};
        if (m.pair === state.activePair) {
          state.secondsLeft = m.seconds_left;
          drawChart();
          updateMicro(m.micro || {});
        }
        break;
      }
      case 'history': {
        state.candles[m.pair] = m.candles || [];
        state.mini[m.pair] = (m.candles || []).slice(-14).map(c => Math.sign(c.close - c.open));
        if (m.pair === state.activePair) drawChart();
        break;
      }
      case 'analysis': {
        // candle closed & scored for this pair
        if (state.candles[m.pair]) state.candles[m.pair].push(m.candle);
        if (!state.mini[m.pair]) state.mini[m.pair] = [];
        state.mini[m.pair].push(Math.sign(m.candle.close - m.candle.open));
        state.mini[m.pair] = state.mini[m.pair].slice(-14);
        if (m.signal) state.feedItems.unshift(m.signal);
        if (m.pair === state.activePair) {
          Views.renderAnalysis(m);
          drawChart();
        }
        Views.renderFeed(state.feedItems);
        refreshHistorySoon();
        break;
      }
      case 'result': {
        const sig = m.signal;
        const i = state.feedItems.findIndex(s => s.id === sig.id);
        if (i >= 0) state.feedItems[i] = sig; else state.feedItems.unshift(sig);
        Views.renderFeed(state.feedItems);
        refreshHistorySoon();
        refreshStatsSoon();
        if (state._tab === 'home') Views.renderHome(state);
        break;
      }
    }
  }

  let _histTimer = null, _statsTimer = null;
  function refreshHistorySoon() {
    clearTimeout(_histTimer);
    _histTimer = setTimeout(() => { if (state._tab === 'history') loadHistory(); }, 700);
  }
  function refreshStatsSoon() {
    clearTimeout(_statsTimer);
    _statsTimer = setTimeout(() => { if (state._tab === 'stats') loadStats(); }, 900);
  }

  function updateConnBadge() {
    const st = state.status;
    const dot = $('conn-dot'), txt = $('conn-text');
    if (st.feed === 'live' && st.connected && st.authenticated) {
      dot.className = 'dot dot-live';
      txt.textContent = `${st.broker || 'Quotex'} · ${st.mode === 'real' ? 'রিয়েল' : 'ডেমো'} · কানেক্টেড`;
    } else if (st.feed === 'demo') {
      dot.className = 'dot dot-demo';
      txt.textContent = 'ডেমো ফিড (টোকেন নেই)';
    } else if (st.connected) {
      dot.className = 'dot dot-demo';
      txt.textContent = 'অথেন্টিকেট হচ্ছে…';
    } else {
      dot.className = 'dot dot-off';
      txt.textContent = 'ডিসকানেক্টেড — রিকানেক্ট হচ্ছে';
    }
  }

  // ------------------------------------------------------------ chart
  function drawChart() {
    if (!chart) return;
    const pair = state.activePair;
    const candles = state.candles[pair] || [];
    const running = state.running[pair] || null;
    const pend = (state.status.pending || {})[pair];
    const marks = state.feedItems.filter(s => s.pair === pair).map(s => ({
      minute: s.minute - 60, dir: s.direction, result: s.result,
    }));
    chart.setMarks(marks);
    chart.setData(candles, running, state.secondsLeft, pend ? pend.entry : null);
  }

  function updateMicro(micro) {
    const fill = $('micro-fill'), val = $('micro-val'), flip = $('flip-badge');
    const bias = micro.bias || 0;
    fill.style.left = `${50 + bias * 50}%`;
    val.textContent = `${bias > 0 ? '+' : ''}${Math.round(bias * 100)}%`;
    val.style.color = bias > 0.15 ? 'var(--green)' : bias < -0.15 ? 'var(--red)' : 'var(--dim)';
    flip.hidden = !micro.flip_risk;
    const cd = $('countdown');
    cd.textContent = `${String(micro.seconds_left || state.secondsLeft).padStart(2, '0')}s`;
    cd.style.color = (micro.seconds_left || 60) <= 10 ? 'var(--red)' : 'var(--accent)';
  }

  // ------------------------------------------------------------ data loads
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  async function loadCandles() {
    const pair = state.activePair;
    if (!pair) return;
    try {
      const d = await api(`/api/candles/${encodeURIComponent(pair)}?limit=150`);
      state.candles[pair] = d.candles || [];
      state.running[pair] = d.running;
      state.micro[pair] = d.micro || {};
      state.secondsLeft = d.seconds_left || 60;
      drawChart();
      updateMicro(d.micro || {});
    } catch (e) { /* ignore */ }
  }

  async function loadHistory() {
    const w = $('hist-window').value;
    const pair = $('hist-pair').value || null;
    const dir = $('hist-dir').value || null;
    const res = $('hist-result').value || null;
    const qs = new URLSearchParams({ window: w });
    if (pair) qs.set('pair', pair);
    if (dir) qs.set('direction', dir);
    if (res) qs.set('result', res);
    try { Views.renderHistory(await api(`/api/signals?${qs}`)); } catch (e) {}
  }

  async function loadStats() {
    try {
      const d = await api(`/api/stats?window=${$('stats-window').value}`);
      state.stats = d;
      Views.renderStats(d);
    } catch (e) {}
  }

  async function loadStatusOnce() {
    try {
      const d = await api('/api/status');
      state.status = d;
      state.known_pairs = d.known_pairs || [];
      if (!state.activePair && d.pairs && d.pairs.length) state.activePair = d.pairs[0].pair;
      updateConnBadge();
      Views.renderPairTabs(state);
      Views.renderHome(state);
      const s = await api('/api/settings');
      state.settingsMasked = s;
      Views.renderSettings(state);
      // seed the feed with recent history so late joiners see context
      try {
        const hist = await api('/api/signals?window=1h&limit=25');
        state.feedItems = (hist.signals || []).map(r => ({
          id: r.id, pair: r.pair, direction: r.direction, confidence: r.confidence,
          score: r.score, entry: r.entry, close: r.close, result: r.result,
          created_at: r.created_at, tier: r.confidence >= 78 ? 'strong' : r.confidence >= 63 ? 'medium' : 'weak',
          factors: r.factors || [], minute: r.minute,
        }));
        Views.renderFeed(state.feedItems);
        // show pending signal in live panel if any for active pair
        const pend = (d.pending || {})[state.activePair];
        if (pend) Views.renderAnalysis({ pair: state.activePair, score: pend.score, confidence: pend.confidence, factors: pend.factors, signal: pend });
      } catch (e) { /* history optional */ }
    } catch (e) {}
  }

  // ------------------------------------------------------------ tabs
  function goTab(name) {
    state._tab = name;
    document.querySelectorAll('.tab').forEach(el => el.hidden = true);
    $(`tab-${name}`).hidden = false;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    if (name === 'signals') { if (chart) chart.resize(); loadCandles(); }
    if (name === 'history') loadHistory();
    if (name === 'stats') loadStats();
    if (name === 'settings') Views.renderSettings(state);
  }
  window.App = { goTab, selectPair };

  function selectPair(pair) {
    state.activePair = pair;
    Views.renderPairTabs(state);
    loadCandles();
    if (state._tab !== 'signals') goTab('signals');
  }

  // ------------------------------------------------------------ settings
  function bindSettings() {
    const tok = $('set-token');
    tok.addEventListener('input', () => { state._tokenEdited = true; });
    $('token-show').onclick = () => {
      tok.type = tok.type === 'password' ? 'text' : 'password';
    };
    $('set-conf').oninput = (e) => { $('conf-val').textContent = e.target.value; };

    $('set-save').onclick = async () => {
      const msg = $('save-msg');
      msg.style.color = 'var(--accent)';
      msg.textContent = 'সেভ হচ্ছে…';
      const pairs = [...document.querySelectorAll('#set-pairs input:checked')].map(i => i.value);
      const body = {
        is_demo: parseInt($('set-demo').value, 10),
        pairs,
        min_confidence: parseInt($('set-conf').value, 10),
      };
      if (state._tokenEdited && tok.value.trim()) body.token = tok.value.trim();
      try {
        const d = await api('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        state.settingsMasked = d;
        state._tokenEdited = false;
        Views.renderSettings(state);
        msg.style.color = 'var(--green)';
        msg.textContent = '✓ সেভ হয়েছে — কানেক্ট হচ্ছে…';
        setTimeout(loadStatusOnce, 2500);
      } catch (e) {
        msg.style.color = 'var(--red)';
        msg.textContent = '✗ সেভ ব্যর্থ: ' + e.message;
      }
    };

    // history filters
    ['hist-window', 'hist-pair', 'hist-dir', 'hist-result'].forEach(id => {
      $(id).onchange = loadHistory;
    });
    $('stats-window').onchange = loadStats;

    // backtest
    $('bt-run').onclick = async () => {
      const btn = $('bt-run');
      btn.disabled = true; btn.textContent = 'চলছে…';
      try {
        const d = await api('/api/backtest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        Views.renderBacktest(d);
      } catch (e) {
        Views.renderBacktest({ error: 'ব্যাকটেস্ট ব্যর্থ: ' + e.message });
      } finally {
        btn.disabled = false; btn.textContent = 'ব্যাকটেস্ট চালাও';
      }
    };
  }

  // ------------------------------------------------------------ init
  function init() {
    chart = new CandleChart($('chart-canvas'));
    chart.onOhlc = (label) => { $('ohlc-panel').textContent = label; };
    document.querySelectorAll('.tab-btn').forEach(b => b.onclick = () => goTab(b.dataset.tab));

    // countdown ticker (client-side, 100ms)
    setInterval(() => {
      state.secondsLeft = Math.max(0, state.secondsLeft - 0.1);
      const cd = $('countdown');
      const s = Math.ceil(state.secondsLeft);
      cd.textContent = `${String(s).padStart(2, '0')}s`;
      cd.style.color = s <= 10 ? 'var(--red)' : 'var(--accent)';
      if (chart && chart.running) chart.draw();
    }, 250);

    bindSettings();
    connectWS();
    loadStatusOnce();
    loadStats();
    Views.renderFeed([]);
    setInterval(() => {
      if (state._tab === 'home') Views.renderHome(state);
      if (state._tab === 'stats') loadStats();
    }, 15000);
    setInterval(loadCandles, 30000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
