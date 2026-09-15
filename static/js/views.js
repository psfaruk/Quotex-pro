/* View renderers — all DOM building for the 5 tabs */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtPrice(p) {
    if (p == null) return '—';
    if (p >= 100) return p.toFixed(3);
    if (p >= 10) return p.toFixed(4);
    return p.toFixed(5);
  }
  function fmtTime(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function fmtMinute(m) {
    return new Date(m * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  }
  function fmtAmt(v) {
    if (v == null) return '—';
    return Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });
  }
  function dirClass(d) { return d === 'CALL' ? 'call' : 'put'; }

  // ------------------------------------------------------------- HOME
  window.Views = {};

  Views.renderHome = function (state) {
    const st = state.status || {};
    // KPIs
    const stats = state.stats || {};
    const t = stats.total || {};
    $('kpi-total').textContent = t.win != null ? `${(t.win || 0) + (t.loss || 0) + (t.tie || 0)}` : '—';
    $('kpi-winrate').textContent = t.winrate != null ? `${t.winrate}%` : '—';
    const bal = st.balance || {};
    $('kpi-balance').textContent = st.mode === 'real' ? `$${fmtAmt(bal.liveBalance)}` : `$${fmtAmt(bal.demoBalance)}`;
    $('kpi-feed').textContent = st.feed === 'live' ? (st.broker || 'Live') : 'ডেমো ফিড';

    // pair cards
    const grid = $('home-pairs');
    const pairs = st.pairs || [];
    if (!pairs.length) { grid.innerHTML = '<div class="empty">সেটিংস ট্যাবে টোকেন দিন / পেয়ার সিলেক্ট করুন</div>'; }
    else {
      grid.innerHTML = pairs.map(p => {
        const mini = (state.mini || {})[p.pair] || [];
        const pend = (state.status.pending || {})[p.pair];
        const lastSig = pend ? `<span class="${pend.direction === 'CALL' ? 'g' : 'r'}">${pend.direction}</span>` : '';
        const miniBars = mini.map(h => `<i class="${h < 0 ? 'd' : ''}" style="height:${h === 0 ? 4 : Math.min(24, Math.abs(h) * 18 + 4)}px"></i>`).join('');
        return `
        <div class="pair-card" data-pair="${esc(p.pair)}">
          <div class="pc-top">
            <span class="pc-name">${esc(p.pair.replace('_otc', ''))}${p.pair.includes('_otc') ? ' <small style="color:var(--dim2);font-size:10px">OTC</small>' : ''}</span>
            <span class="pc-pay">${p.payout ? 'Payout ' + p.payout + '%' : ''}</span>
          </div>
          <div class="pc-mid">
            <span class="pc-price">${fmtPrice(p.price)}</span>
            <span class="pc-dir ${p.dir > 0 ? 'up' : p.dir < 0 ? 'dn' : 'flat'}">${p.dir > 0 ? '▲' : p.dir < 0 ? '▼' : '•'}</span>
            ${lastSig}
          </div>
          <div class="pc-mini">${miniBars}</div>
          <div class="pc-bottom"><span>ক্যান্ডেল: ${p.candles || 0}</span><span>${p.seconds_left != null ? p.seconds_left + 's' : ''}</span></div>
          <div class="pc-bar"><b style="width:${Math.max(0, 60 - (p.seconds_left || 60)) / 60 * 100}%"></b></div>
        </div>`;
      }).join('');
      grid.querySelectorAll('.pair-card').forEach(el => {
        el.onclick = () => { App.goTab('signals'); App.selectPair(el.dataset.pair); };
      });
    }

    // pending signals
    const pend = $('home-pending');
    const pendList = Object.values(st.pending || {});
    if (!pendList.length) {
      pend.innerHTML = '<div class="empty">এই মুহূর্তে কোনো সিগন্যাল রান করছে না — ক্যান্ডেল ক্লোজে কনফ্লুয়েন্স মিললে সিগন্যাল আসবে।</div>';
    } else {
      pend.innerHTML = pendList.map(s => Views.signalCard(s, true)).join('');
    }
  };

  Views.signalCard = function (s, withCountdown) {
    const run = !s.result;
    return `
    <div class="sig-card ${dirClass(s.direction).toLowerCase() === 'call' ? 'call' : 'put'}">
      <div class="sig-head">
        <span class="sig-dir ${s.direction === 'CALL' ? 'call' : 'put'}">${s.direction === 'CALL' ? '⬆ CALL' : '⬇ PUT'}</span>
        <span class="sig-conf"><b>${s.confidence}%</b> · ${esc(s.pair.replace('_otc', ''))}</span>
      </div>
      <div class="confbar"><i style="width:${s.confidence}%"></i></div>
      <div class="sig-meta">
        <span>এন্ট্রি <b class="mono">${fmtPrice(s.entry)}</b></span>
        <span>টিয়ার <b>${s.tier || ''}</b></span>
        <span>স্কোর <b class="mono">${s.score}</b></span>
        ${run ? `<span class="cd-inline" data-cd="${s.created_at}"></span>` : `<span>ক্লোজ <b class="mono">${fmtPrice(s.close)}</b></span>`}
      </div>
      ${run
        ? '<div class="sig-status run">⏳ রানিং — ক্যান্ডেল ক্লোজে রেজাল্ট</div>'
        : `<div class="sig-status ${s.result.toLowerCase()}">${s.result === 'WIN' ? '✓ WIN' : s.result === 'LOSS' ? '✗ LOSS' : '— TIE'}</div>`}
    </div>`;
  };

  // ------------------------------------------------------------- SIGNALS
  Views.renderPairTabs = function (state) {
    const tabs = $('pair-tabs');
    const pairs = (state.status.pairs || []).map(p => p.pair);
    if (!pairs.length) return;
    tabs.innerHTML = pairs.map(p => {
      const act = state.activePair === p ? ' active' : '';
      return `<button class="pair-tab${act}" data-pair="${esc(p)}">${esc(p.replace('_otc', ''))}</button>`;
    }).join('');
    tabs.querySelectorAll('.pair-tab').forEach(el => {
      el.onclick = () => App.selectPair(el.dataset.pair);
    });
  };

  Views.renderAnalysis = function (a) {
    // a = {pair, minute, score, confidence, factors, signal, reason}
    const live = $('live-signal');
    if (a.signal) {
      live.innerHTML = Views.signalCard(a.signal, true);
    } else {
      live.innerHTML = `<div class="sig-card">
        <div class="sig-head"><span class="sig-conf">কনফ্লুয়েন্স স্কোর: <b>${a.score}</b> · কনফিডেন্স <b>${a.confidence}%</b></span></div>
        <div class="confbar"><i style="width:${Math.min(a.confidence, 100)}%; background:${Math.abs(a.score) < 35 ? 'linear-gradient(90deg,#5a6785,#8391ad)' : ''}"></i></div>
        <div class="sig-status run" style="color:var(--dim)">${esc(a.reason || 'NO SIGNAL — কনফ্লুয়েন্স অপর্যাপ্ত')}</div>
      </div>`;
    }
    const fl = $('factor-list');
    const facs = a.factors || [];
    if (!facs.length) { fl.innerHTML = '<div class="empty">এই ক্যান্ডেলে কোনো স্কোরযোগ্য ফ্যাক্টর নেই</div>'; return; }
    fl.innerHTML = facs.map(f => {
      const pct = Math.min(Math.abs(f.score) / 26 * 100, 100) / 2;
      const pos = f.score >= 0;
      return `<div class="factor">
        <span class="f-name">${esc(f.name)}</span>
        <span class="f-text">${esc(f.text)}</span>
        <div class="f-bar"><i class="${pos ? 'p' : 'n'}" style="width:${pct}%"></i></div>
        <span class="f-score ${pos ? 'p' : 'n'}">${f.score > 0 ? '+' : ''}${f.score}</span>
      </div>`;
    }).join('');
  };

  Views.renderFeed = function (feedItems) {
    const el = $('signal-feed');
    if (!feedItems.length) { el.innerHTML = '<div class="empty">ক্যান্ডেল ক্লোজে সিগন্যাল/অ্যানালাইসিস এখানে জমা হবে…</div>'; return; }
    el.innerHTML = feedItems.slice(0, 40).map(s => {
      const res = s.result || 'RUN';
      return `<div class="feed-item">
        <span class="fi-time">${fmtTime(s.created_at)}</span>
        <span class="fi-pair">${esc(s.pair.replace('_otc', ''))}</span>
        <span class="fi-dir ${s.direction === 'CALL' ? 'call' : 'put'}">${s.direction}</span>
        <span class="fi-conf">${s.confidence}%</span>
        <span class="mono" style="color:var(--dim)">${fmtPrice(s.entry)} → ${fmtPrice(s.close)}</span>
        <span class="fi-res ${res}">${res === 'RUN' ? '⏳ রানিং' : res}</span>
      </div>`;
    }).join('');
  };

  // ------------------------------------------------------------- HISTORY
  Views.renderHistory = function (data) {
    const sum = $('hist-summary');
    const rows = data.signals || [];
    const w = rows.filter(r => r.result === 'WIN').length;
    const l = rows.filter(r => r.result === 'LOSS').length;
    const t = rows.filter(r => r.result === 'TIE').length;
    const wr = (w + l) ? Math.round(100 * w / (w + l)) : 0;
    sum.innerHTML = `<span>মোট <b>${rows.length}</b></span><span>উইন <b style="color:var(--green)">${w}</b></span>
      <span>লস <b style="color:var(--red)">${l}</b></span><span>টাই <b>${t}</b></span>
      <span>উইন রেট <b style="color:var(--accent)">${wr}%</b></span>`;

    const body = $('hist-body');
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">এই ফিল্টারে কোনো সিগন্যাল নেই</td></tr>`;
      return;
    }
    body.innerHTML = rows.map(r => `
      <tr>
        <td class="mono">${fmtTime(r.created_at)}</td>
        <td><b>${esc(r.pair.replace('_otc', ''))}</b>${r.pair.includes('_otc') ? ' <small style="color:var(--dim2)">OTC</small>' : ''}</td>
        <td><span class="pill ${r.direction}">${r.direction}</span></td>
        <td class="mono">${r.confidence}% <small style="color:var(--dim2)">${r.score > 0 ? '+' : ''}${r.score}</small></td>
        <td class="mono">${fmtPrice(r.entry)}</td>
        <td class="mono">${fmtPrice(r.close)}</td>
        <td>${r.result ? `<span class="pill ${r.result}">${r.result}</span>` : '<span class="pill RUN">RUN</span>'}</td>
      </tr>`).join('');
  };

  Views.fillHistoryPairSelect = function (pairs) {
    const sel = $('hist-pair');
    const cur = sel.value;
    sel.innerHTML = '<option value="">সব পেয়ার</option>' + pairs.map(p => `<option value="${esc(p)}">${esc(p.replace('_otc', ''))}${p.includes('_otc') ? ' OTC' : ''}</option>`).join('');
    if (pairs.includes(cur)) sel.value = cur;
  };

  // ------------------------------------------------------------- STATS
  Views.renderStats = function (data) {
    const t = data.total || {};
    const kpis = $('stats-kpis');
    kpis.innerHTML = `
      <div class="kpi"><div class="kpi-label">মোট সিগন্যাল</div><div class="kpi-value">${t.signals || 0}</div></div>
      <div class="kpi"><div class="kpi-label">উইন রেট</div><div class="kpi-value accent">${t.winrate || 0}%</div></div>
      <div class="kpi"><div class="kpi-label">উইন / লস</div><div class="kpi-value" style="font-size:18px"><span style="color:var(--green)">${t.win || 0}</span> / <span style="color:var(--red)">${t.loss || 0}</span></div></div>
      <div class="kpi"><div class="kpi-label">টাই</div><div class="kpi-value">${t.tie || 0}</div></div>`;

    const pb = $('pair-stats-body');
    const pp = Object.entries(data.per_pair || {}).sort((a, b) => (b[1].winrate || 0) - (a[1].winrate || 0));
    if (!pp.length) { pb.innerHTML = '<tr><td colspan="7" class="empty">এখনো সিগন্যাল জমেনি</td></tr>'; }
    else {
      pb.innerHTML = pp.map(([p, s]) => `
        <tr>
          <td><b>${esc(p.replace('_otc', ''))}</b>${p.includes('_otc') ? ' <small style="color:var(--dim2)">OTC</small>' : ''}</td>
          <td>${s.signals || 0}</td>
          <td style="color:var(--green)">${s.win || 0}</td>
          <td style="color:var(--red)">${s.loss || 0}</td>
          <td><b>${s.winrate || 0}%</b></td>
          <td>${s.call_winrate || 0}% <small style="color:var(--dim2)">(${(s.call_win || 0) + (s.call_loss || 0)})</small></td>
          <td>${s.put_winrate || 0}% <small style="color:var(--dim2)">(${(s.put_win || 0) + (s.put_loss || 0)})</small></td>
        </tr>`).join('');
    }

    const tc = $('tier-cards');
    const tiers = ['strong', 'medium', 'weak'];
    const names = { strong: 'STRONG (78%+)', medium: 'MEDIUM (63-77%)', weak: 'WEAK (50-62%)' };
    tc.innerHTML = tiers.map(tr => {
      const s = (data.per_tier || {})[tr] || {};
      return `<div class="tier-card">
        <div class="tier-name">${names[tr]}</div>
        <div class="tier-wr" style="color:${(s.winrate || 0) >= 55 ? 'var(--green)' : (s.winrate || 0) >= 50 ? 'var(--accent)' : 'var(--red)'}">${s.winrate || 0}%</div>
        <div class="tier-sub">${s.win || 0}W / ${s.loss || 0}L</div>
      </div>`;
    }).join('') || '<div class="empty">ডেটা নেই</div>';

    const db2 = $('dir-bars');
    const cd = (data.per_direction || {}).CALL || {}, pd = (data.per_direction || {}).PUT || {};
    function bar(name, cls, d) {
      const wr = d.winrate || 0;
      return `<div class="dir-bar">
        <span class="dir-name ${cls}">${name}</span>
        <div class="db-track"><div class="db-fill ${cls}" style="width:${Math.max(wr, 2)}%">${wr}%</div></div>
        <span class="dir-count">${d.win || 0}W / ${d.loss || 0}L</span>
      </div>`;
    }
    db2.innerHTML = bar('CALL', 'call', cd) + bar('PUT', 'put', pd);
  };

  Views.renderBacktest = function (bt) {
    const el = $('bt-result');
    if (bt.error) { el.innerHTML = `<div class="empty">${esc(bt.error)}</div>`; return; }
    const t = bt.total || {};
    const rows = Object.entries(bt.pairs || {});
    el.innerHTML = `
      <div class="bt-total">
        <div class="bt-kpi"><div class="l">টেস্টেড সিগন্যাল</div><div class="v">${t.signals || 0}</div></div>
        <div class="bt-kpi"><div class="l">উইন রেট</div><div class="v" style="color:${(t.winrate || 0) >= 55 ? 'var(--green)' : 'var(--accent)'}">${t.winrate || 0}%</div></div>
        <div class="bt-kpi"><div class="l">CALL / PUT</div><div class="v" style="font-size:14px">${t.call_winrate || 0}% / ${t.put_winrate || 0}%</div></div>
        <div class="bt-kpi"><div class="l">STRONG টিয়ার</div><div class="v" style="font-size:14px">${(t.strong || {}).winrate || 0}%</div></div>
      </div>
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>পেয়ার</th><th>ক্যান্ডেল</th><th>সিগন্যাল</th><th>উইন রেট</th><th>CALL</th><th>PUT</th><th>Strong</th></tr></thead>
        <tbody>${rows.map(([p, s]) => s.error
          ? `<tr><td>${esc(p)}</td><td colspan="6" style="color:var(--dim2)">${esc(s.error)}</td></tr>`
          : `<tr>
              <td><b>${esc(p.replace('_otc', ''))}</b></td>
              <td>${s.candles_tested || 0}</td>
              <td>${s.signals || 0}</td>
              <td><b>${s.winrate || 0}%</b> (${s.win || 0}W/${s.loss || 0}L)</td>
              <td>${s.call_winrate || 0}%</td>
              <td>${s.put_winrate || 0}%</td>
              <td>${(s.strong || {}).winrate || 0}%</td>
            </tr>`).join('')}
        </tbody></table></div>
      <div class="help" style="margin-top:10px">⚠ ব্যাকটেস্ট = একই ইঞ্জিন হিস্ট্রিক্যাল ক্যান্ডেলে রিপ্লে। এটা ভবিষ্যতের গ্যারান্টি না — শুধু ইঞ্জিনের আচরণ যাচাই করে। ডেটা যত বেশি জমবে, রেজাল্ট তত বিশ্বাসযোগ্য।</div>`;
  };

  // ------------------------------------------------------------- SETTINGS
  Views.renderSettings = function (state) {
    const m = state.settingsMasked || {};
    const st = state.status || {};
    $('set-demo').value = String(m.is_demo != null ? m.is_demo : 1);
    if (!state._tokenEdited) {
      $('set-token').value = '';
      $('set-token').placeholder = m.token_set ? `সেভ করা আছে: ${m.token_masked}` : 'ssid কুকি থেকে টোকেন পেষ্ট করুন';
    }
    $('set-conf').value = m.min_confidence || 55;
    $('conf-val').textContent = m.min_confidence || 55;
    const checks = $('set-pairs');
    const known = state.known_pairs || [];
    const sel = new Set(m.pairs || []);
    checks.innerHTML = known.map(p => `
      <label class="pc-check"><input type="checkbox" value="${esc(p)}" ${sel.has(p) ? 'checked' : ''}>
      ${esc(p.replace('_otc', ''))}${p.includes('_otc') ? ' <small style="color:var(--dim2)">OTC</small>' : ''}</label>`).join('');
  };

  // utils used by app.js
  Views.utils = { $, esc, fmtPrice, fmtTime, fmtMinute, fmtAmt };
})();
