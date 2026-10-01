/* Guns.lol Sniper dashboard. No secrets live in this file: the server URL and API key are entered by the
   user at runtime and kept in the iOS Keychain (native app) or this browser's localStorage (Safari). */
(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const S = { url: '', key: '', connected: false, visible: true, tab: 'dash', status: null, statusAt: 0,
  online: null, latency: null, logs: [], lastId: 0, epoch: 0, filter: 'all', config: null, draft: {}, secretClear: {} };

/* ---------------------------------------------------------------- secure storage */
const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.secureStore;
const store = {
  async get(k) {
    if (bridge) { try { return (await bridge.postMessage({ op: 'get', key: k })) || null; } catch (e) { return null; } }
    try { return localStorage.getItem('gs_' + k); } catch (e) { return null; }
  },
  async set(k, v) {
    if (bridge) { try { await bridge.postMessage({ op: 'set', key: k, value: v }); } catch (e) {} return; }
    try { localStorage.setItem('gs_' + k, v); } catch (e) {}
  },
  async del(k) {
    if (bridge) { try { await bridge.postMessage({ op: 'delete', key: k }); } catch (e) {} return; }
    try { localStorage.removeItem('gs_' + k); } catch (e) {}
  },
};

/* ---------------------------------------------------------------- helpers */
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
function fmtDur(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m ${String(x).padStart(2, '0')}s`;
  if (m) return `${m}m ${String(x).padStart(2, '0')}s`;
  return `${x}s`;
}
function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 3 ? 'just now' : fmtDur(s) + ' ago';
}
function clock(ts) { const d = new Date(ts * 1000); return [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, '0')).join(':'); }
function normUrl(raw) {
  let u = (raw || '').trim();
  if (!u) return null;
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
  let p; try { p = new URL(u); } catch (e) { return null; }
  const local = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|.*\.local$)/.test(p.hostname);
  if (p.protocol === 'http:' && !local) return null;     // API key must never cross the internet in clear text
  return p.origin + p.pathname.replace(/\/+$/, '');
}
function toast(msg, kind) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast ' + (kind || ''); 
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hidden'), 3200);
}
function setBusy(btn, on) { btn.classList.toggle('busy', on); }

/* sheets (custom: native alert/confirm are unavailable in a bare WKWebView) */
function openSheet(build) {
  const bg = $('#sheet-bg'), sh = $('#sheet'); sh.replaceChildren();
  const close = () => bg.classList.add('hidden');
  build(sh, close); bg.classList.remove('hidden');
  bg.onclick = e => { if (e.target === bg) close(); };
  return close;
}
function confirmSheet(title, msg, okLabel, danger) {
  return new Promise(res => openSheet((sh, close) => {
    sh.append(el('h3', '', title), el('p', '', msg));
    const b = el('div', 'btns');
    const ok = el('button', 'btn big ' + (danger ? 'stop' : 'primary'), okLabel); ok.onclick = () => { close(); res(true); };
    const no = el('button', 'btn ghost', 'Cancel'); no.onclick = () => { close(); res(false); };
    b.append(ok, no); sh.append(b);
  }));
}

/* ---------------------------------------------------------------- API */
class ApiError extends Error { constructor(code, msg, data) { super(msg); this.code = code; this.data = data; } }
async function api(path, o = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), o.timeout || 15000);
  if (o.signal) o.signal.addEventListener('abort', () => ctl.abort());
  const t0 = performance.now();
  let r;
  try {
    r = await fetch(S.url + path, {
      method: o.method || 'GET', cache: 'no-store', signal: ctl.signal,
      headers: Object.assign({ Authorization: 'Bearer ' + S.key }, o.body ? { 'Content-Type': 'application/json' } : {}),
      body: o.body ? JSON.stringify(o.body) : undefined,
    });
  } catch (e) {
    if (!(o.signal && o.signal.aborted)) setOnline(false);
    throw new ApiError(0, o.signal && o.signal.aborted ? 'aborted' : 'Cannot reach the server');
  } finally { clearTimeout(timer); }
  let data = null; try { data = await r.json(); } catch (e) {}
  if (o.track !== false) setOnline(true, Math.round(performance.now() - t0)); else if (!S.online) setOnline(true, S.latency);
  if (r.status === 401) { if (S.connected) signOut('Your API key was rejected. Please reconnect.'); throw new ApiError(401, (data && data.error) || 'Invalid API key', data); }
  if (!r.ok) throw new ApiError(r.status, (data && data.error) || 'Request failed', data);
  return data;
}
function setOnline(on, ms) { const changed = S.online !== on; S.online = on; if (ms != null) S.latency = ms; if (changed) render(); }

/* ---------------------------------------------------------------- connect / sign-out */
async function connect(url, key, fromUser) {
  const n = normUrl(url);
  const err = $('#connect-err');
  err.classList.add('hidden');
  if (!n) { if (fromUser) showConnectErr('Enter a valid https:// server address.'); return false; }
  if (!key) { if (fromUser) showConnectErr('Enter your API key.'); return false; }
  S.url = n; S.key = key.trim();
  try {
    S.status = await api('/api/status'); S.statusAt = Date.now();
  } catch (e) {
    if (e.code === 401) { showConnectErr('Wrong API key.'); return false; }
    if (e.code === 429) { showConnectErr(e.message); return false; }
    if (fromUser) { showConnectErr('Cannot reach ' + n + '. Check the address and that the server is running over HTTPS.'); return false; }
    // stored credentials but server temporarily unreachable: still open the app so state is visible
  }
  if (fromUser) { await store.set('url', S.url); await store.set('key', S.key); }
  S.connected = true;
  $('#connect').classList.add('hidden'); $('#app').classList.remove('hidden');
  $('#in-key').value = '';
  startLoops(); render(); loadResults(); loadConfig();
  return true;
}
function showConnectErr(m) { const e = $('#connect-err'); e.textContent = m; e.classList.remove('hidden'); }
async function signOut(msg) {
  stopLoops(); S.connected = false; S.key = ''; S.logs = []; S.lastId = 0; S.epoch = 0; S.status = null; S.config = null;
  await store.del('key');
  $('#app').classList.add('hidden'); $('#connect').classList.remove('hidden');
  $('#in-url').value = S.url || $('#in-url').value;
  if (msg) showConnectErr(msg);
}

/* ---------------------------------------------------------------- polling loops */
let statusTimer = null, tickTimer = null, logToken = 0, logAbort = null;
function startLoops() {
  stopLoops();
  pollStatus();
  statusTimer = setInterval(() => { if (S.visible && S.connected) pollStatus(); }, 3000);
  tickTimer = setInterval(tick, 1000);
  logLoop();
}
function stopLoops() {
  clearInterval(statusTimer); clearInterval(tickTimer); logToken++;
  if (logAbort) logAbort.abort();
}
async function pollStatus() {
  try { S.status = await api('/api/status'); S.statusAt = Date.now(); } catch (e) {}
  render();
}
async function logLoop() {
  const token = ++logToken;
  if (logAbort) logAbort.abort();
  logAbort = new AbortController();
  const signal = logAbort.signal;
  while (token === logToken && S.connected && S.visible) {
    try {
      const r = await api(`/api/logs?since=${S.lastId}&limit=${S.lastId ? 300 : 400}&wait=20`, { timeout: 30000, track: false, signal });
      if (token !== logToken) return;
      let rebuild = false;
      if (r.epoch !== S.epoch) { S.logs = []; S.epoch = r.epoch; rebuild = true; }
      if (r.entries.length && r.entries[0].id <= S.lastId) { S.logs = []; rebuild = true; }   // server reset
      S.lastId = r.last_id;
      if (r.entries.length || rebuild) addLogs(r.entries, rebuild);
    } catch (e) {
      if (token !== logToken || (signal && signal.aborted)) return;
      await sleep(2500);
    }
  }
}
document.addEventListener('visibilitychange', () => {
  S.visible = document.visibilityState === 'visible';
  if (!S.connected) return;
  if (S.visible) { pollStatus(); logLoop(); } else { logToken++; if (logAbort) logAbort.abort(); }
});

/* ---------------------------------------------------------------- rendering */
const STATE_UI = {
  running: ['Running', 'green'], starting: ['Starting', 'amber'], stopping: ['Stopping', 'amber'],
  stopped: ['Stopped', 'grey'], error: ['Error', 'red'],
};
const GUNS_UI = {
  connected: ['Connected', 'green'], unknown: ['Not checked', 'grey'], blocked: ['Blocked', 'red'],
  rate_limited: ['Rate limited', 'amber'], error: ['Error', 'red'], disconnected: ['Idle', 'grey'],
};
function setDot(id, color) { $(id).className = 'dot ' + color; }
function render() {
  if (!S.connected) return;
  const st = S.status, sn = st && st.sniper;
  const [label, color] = sn ? (STATE_UI[sn.state] || [sn.state, 'grey']) : ['—', 'grey'];

  const pill = $('#pill');
  if (S.online === false) { pill.className = 'pill red'; pill.lastChild.textContent = 'Offline'; }
  else { pill.className = 'pill ' + color; pill.lastChild.textContent = label; }
  $('#offline-banner').classList.toggle('hidden', S.online !== false);

  $('#hero-state').textContent = label; setDot('#hero-dot', color); $('#hero-dot').classList.toggle('pulse', sn && (sn.state === 'running' || sn.state === 'starting'));
  const cs = st && st.config_summary;
  $('#hero-mode').textContent = cs ? `${cs.mode} · ${cs.source}` : '—';
  $('#hero-name').textContent = st && st.current_username ? st.current_username : (sn && sn.running ? '…' : '—');
  let act = st ? st.activity : '—';
  if (sn && (sn.state === 'stopped' || sn.state === 'error') && sn.last_exit) act = ({ stopped: 'Stopped by you', finished: 'Finished its run', crashed: 'Stopped unexpectedly', self_test_failed: 'Start-up check failed' })[sn.last_exit.reason] || act;
  $('#hero-activity').textContent = act;

  const running = !!(sn && sn.running), busy = sn && (sn.state === 'starting' || sn.state === 'stopping');
  $('#btn-start').disabled = running || S.online === false;
  $('#btn-stop').disabled = !running || sn.state === 'stopping' || S.online === false;
  $('#btn-restart').disabled = busy || S.online === false;

  const stats = (st && st.stats) || {};
  $('#st-attempts').textContent = stats.attempts || 0; $('#st-available').textContent = stats.available || 0;
  $('#st-failed').textContent = stats.failed || 0; $('#st-taken').textContent = stats.taken || 0;

  setDot('#c-server-dot', S.online === false ? 'red' : S.online ? 'green' : 'grey');
  $('#c-server').textContent = S.online === false ? 'Unreachable' : S.online ? `Online${S.latency != null ? ' · ' + S.latency + ' ms' : ''}` : '—';
  const g = st && st.guns, gu = g ? (GUNS_UI[g.status] || [g.status, 'grey']) : ['—', 'grey'];
  setDot('#c-guns-dot', gu[1]); $('#c-guns').textContent = gu[0];
  $('#s-url').textContent = S.url; 
  if (st && st.server) { $('#s-version').textContent = 'v' + st.server.version; }
  tick(); renderRecent();
}
function tick() {
  const st = S.status; if (!st) return;
  const drift = (Date.now() - S.statusAt) / 1000;
  $('#c-uptime').textContent = st.sniper.running ? fmtDur(st.sniper.uptime_seconds + drift) : '—';
  $('#c-last').textContent = ago(st.last_activity);
  if (st.server) $('#s-uptime').textContent = fmtDur(st.server.uptime_seconds + drift);
}

/* logs */
const TAGS = { starting: 'START', connected: 'CONN', checking: 'CHECK', available: 'FREE', unavailable: 'TAKEN', success: 'OK',
  error: 'ERR', disconnected: 'DISC', blocked: 'BLOCK', ratelimit: 'LIMIT', config: 'CFG', test: 'TEST', stopping: 'STOP',
  restarting: 'RESTART', end: 'END', info: 'INFO' };
const passes = e => S.filter === 'all' ? true
  : S.filter === 'found' ? (e.event === 'available' || e.event === 'success')
  : (e.level === 'warning' || e.level === 'error' || e.event === 'disconnected');
function logRow(e) {
  const r = el('div', 'll ' + e.level + (e.event === 'checking' ? ' checking' : ''));
  r.append(el('time', '', clock(e.ts)), el('span', 'tag', TAGS[e.event] || 'INFO'), el('span', 'msg', e.msg));
  return r;
}
function addLogs(entries, rebuild) {
  const box = $('#logbox');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  S.logs.push(...entries);
  if (S.logs.length > 600) S.logs.splice(0, S.logs.length - 600);
  if (rebuild) { drawLogs(); } else {
    const empty = box.querySelector('.empty'); if (empty) empty.remove();
    entries.filter(passes).forEach(e => box.append(logRow(e)));
    while (box.childElementCount > 600) box.firstElementChild.remove();
    if (!box.childElementCount) drawLogs();
  }
  if (stick) box.scrollTop = box.scrollHeight;
  renderRecent();
}
function drawLogs() {
  const box = $('#logbox'); box.replaceChildren();
  const rows = S.logs.filter(passes);
  if (!rows.length) box.append(el('div', 'empty', S.logs.length ? 'No matching entries' : 'No log entries yet'));
  else rows.forEach(e => box.append(logRow(e)));
  box.scrollTop = box.scrollHeight;
}
function renderRecent() {
  const box = $('#recent'); box.replaceChildren();
  const rows = S.logs.filter(e => e.event !== 'checking' && e.event !== 'success').slice(-6).reverse();
  if (!rows.length) { box.append(el('div', 'muted small', 'No activity yet')); return; }
  rows.forEach(e => { const r = el('div', 'rl'); r.append(el('time', '', clock(e.ts)), el('span', '', e.msg)); box.append(r); });
}
async function loadResults() {
  try {
    const r = await api('/api/results'); const box = $('#found'); box.replaceChildren();
    if (!r.names.length) { box.append(el('span', 'muted small', 'Nothing found yet')); return; }
    r.names.slice(0, 40).forEach(n => { const a = el('a', '', n); a.href = 'https://guns.lol/' + encodeURIComponent(n); a.target = '_blank'; a.rel = 'noopener'; box.append(a); });
  } catch (e) {}
}

/* ---------------------------------------------------------------- controls */
async function control(path, btn, okMsg) {
  setBusy(btn, true);
  try {
    const r = await api(path, { method: 'POST', body: {} });
    toast(okMsg || r.message, 'ok');
  } catch (e) { toast(e.message, 'err'); }
  finally { setBusy(btn, false); }
  await pollStatus(); setTimeout(pollStatus, 1500); setTimeout(pollStatus, 4000);
}
$('#btn-start').onclick = () => control('/api/sniper/start', $('#btn-start'), 'Sniper starting');
$('#btn-stop').onclick = async () => { if (await confirmSheet('Stop the sniper?', 'It will stop checking usernames until you start it again.', 'Stop sniper', true)) control('/api/sniper/stop', $('#btn-stop'), 'Stopping'); };
$('#btn-restart').onclick = () => control('/api/sniper/restart', $('#btn-restart'), 'Restarting sniper');
$('#btn-test').onclick = async () => {
  const b = $('#btn-test'), lab = $('span', b); setBusy(b, true); lab.textContent = 'Testing…';
  try {
    const r = await api('/api/test-connection', { method: 'POST', body: {}, timeout: 120000 });
    openSheet((sh, close) => {
      sh.append(el('h3', '', r.ok ? 'Connection OK' : 'Connection problem'), el('p', '', r.message));
      (r.steps || []).forEach(s => { const row = el('div', 'step'); row.append(el('i', 'dot ' + (s.ok ? 'green' : 'red')), el('span', '', `${s.label}: ${s.result} (HTTP ${s.http})`)); sh.append(row); });
      if (r.latency_ms) sh.append(el('p', 'small', `Took ${r.latency_ms} ms · mode ${r.mode}`));
      const bt = el('div', 'btns'); const x = el('button', 'btn ghost', 'Close'); x.onclick = close; bt.append(x); sh.append(bt);
    });
  } catch (e) { toast(e.message, 'err'); }
  finally { setBusy(b, false); lab.textContent = 'Test connection'; pollStatus(); }
};
$('#btn-clear').onclick = async () => {
  if (!(await confirmSheet('Clear logs?', 'This removes the log history stored on the server.', 'Clear logs', true))) return;
  try { await api('/api/logs/clear', { method: 'POST', body: {} }); toast('Logs cleared', 'ok'); } catch (e) { toast(e.message, 'err'); }
};
$$('#log-filter button').forEach(b => b.onclick = () => {
  S.filter = b.dataset.f; $$('#log-filter button').forEach(x => x.classList.toggle('on', x === b)); drawLogs();
});

/* ---------------------------------------------------------------- tabs */
function goto(tab) {
  S.tab = tab;
  $$('.tab').forEach(t => t.classList.add('hidden')); $('#tab-' + tab).classList.remove('hidden');
  $$('.tabbar button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  $('main').style.overflowY = tab === 'logs' ? 'hidden' : 'auto';
  if (tab === 'logs') { const box = $('#logbox'); box.scrollTop = box.scrollHeight; }
  if (tab === 'settings') loadConfig();
  if (tab === 'dash') loadResults();
}
$$('.tabbar button').forEach(b => b.onclick = () => goto(b.dataset.tab));
$$('[data-goto]').forEach(b => b.onclick = () => goto(b.dataset.goto));

/* ---------------------------------------------------------------- settings (only options 67.py already supports) */
const FIELDS = [
  { key: 'mode', type: 'seg', label: 'Mode', opts: [['direct', 'Direct'], ['scrapingant', 'ScrapingAnt']],
    help: 'Direct = plain requests, free. ScrapingAnt = hosted browser, needs a key (~10 credits per check).' },
  { key: 'scrapingant_key', type: 'secret', label: 'ScrapingAnt key', help: 'Only used in ScrapingAnt mode. Stored on the server, never shown again.', showIf: c => c.mode === 'scrapingant' },
  { key: 'letter_count', type: 'num', label: 'Username length', help: 'Length of random names (ignored when the custom list is on).', min: 1, max: 30, step: 1 },
  { key: 'delay', type: 'num', label: 'Delay (seconds)', help: 'Pause between checks.', min: 0, max: 600, step: 0.1 },
  { key: 'max_requests', type: 'num', label: 'Max requests', help: 'Safety stop to protect your quota.', min: 1, max: 10000000, step: 1 },
  { key: 'use_customlist', type: 'toggle', label: 'Use custom list', help: 'Check names from your list instead of random ones.', extra: 'list' },
  { key: 'filter_premium', type: 'toggle', label: 'Skip premium aliases', help: 'Skip names starting/ending with . _ -' },
  { key: 'save_to_file', type: 'toggle', label: 'Save found names', help: 'Append available names to unclaimed.txt on the server.' },
  { key: 'known_taken', type: 'text', label: 'Known taken username', help: 'A name you know is claimed — enables the full start-up self-test.' },
  { key: 'webhook_url', type: 'secret', label: 'Discord webhook', help: 'Get a Discord message when a name is available. Stored on the server, never shown again.' },
];
async function loadConfig() {
  try { const r = await api('/api/config'); S.config = r.config; S.draft = {}; S.secretClear = {}; drawSettings(); } catch (e) {}
}
const val = k => (k in S.draft ? S.draft[k] : S.config[k]);
function touch(k, v) { S.draft[k] = v; $('#btn-save').disabled = false; $('#settings-err').classList.add('hidden'); }
function drawSettings() {
  const form = $('#settings-form'); form.replaceChildren(); if (!S.config) return;
  const cfg = () => Object.assign({}, S.config, S.draft);
  FIELDS.forEach(f => {
    if (f.showIf && !f.showIf(cfg())) return;
    const w = el('div', 'f'), head = el('div', 'f-head');
    head.append(el('label', 't', f.label)); w.append(head);
    if (f.type === 'toggle') {
      const sw = el('label', 'switch'), i = el('input'); i.type = 'checkbox'; i.checked = !!val(f.key);
      i.onchange = () => { touch(f.key, i.checked); };
      sw.append(i, el('i')); head.append(sw);
    }
    if (f.help) w.append(el('div', 'help', f.help));
    if (f.type === 'seg') {
      const sg = el('div', 'seg');
      f.opts.forEach(([v, l]) => { const b = el('button', val(f.key) === v ? 'on' : '', l); b.onclick = () => { touch(f.key, v); drawSettings(); }; sg.append(b); });
      w.append(sg);
    } else if (f.type === 'num' || f.type === 'text') {
      const i = el('input'); i.type = f.type === 'num' ? 'number' : 'text'; i.value = val(f.key);
      if (f.type === 'num') { i.inputMode = 'decimal'; i.min = f.min; i.max = f.max; i.step = f.step; }
      else { i.autocapitalize = 'off'; i.spellcheck = false; }
      i.oninput = () => touch(f.key, f.type === 'num' ? (i.value === '' ? '' : Number(i.value)) : i.value);
      w.append(i);
    } else if (f.type === 'secret') {
      const isSet = S.config[f.key + '_set'] && !S.secretClear[f.key];
      const i = el('input'); i.type = 'password'; i.autocomplete = 'off'; i.spellcheck = false; i.autocapitalize = 'off';
      i.placeholder = isSet ? '•••••••• saved — type to replace' : 'Not set';
      i.oninput = () => { if (i.value) touch(f.key, i.value); else { delete S.draft[f.key]; } };
      w.append(i);
      if (isSet) { const a = el('div', 'inline-actions'); const rm = el('button', 'btn ghost small', 'Remove saved value'); rm.onclick = () => { S.secretClear[f.key] = true; touch(f.key, ''); drawSettings(); }; a.append(rm); w.append(a); }
      else if (S.secretClear[f.key]) w.append(el('div', 'help', 'Will be removed when you save.'));
      if (f.key === 'scrapingant_key' && S.config.scrapingant_key_from_env) w.append(el('div', 'help', 'A key is also provided by the SCRAPINGANT_KEY environment variable on the server.'));
    }
    if (f.extra === 'list') { const a = el('div', 'inline-actions'); const b = el('button', 'btn ghost small', 'Edit custom list'); b.onclick = editList; a.append(b); w.append(a); }
    form.append(w);
  });
}
$('#btn-save').onclick = async () => {
  const b = $('#btn-save'), err = $('#settings-err'); err.classList.add('hidden');
  const patch = Object.assign({}, S.draft);
  for (const k of ['letter_count', 'delay', 'max_requests']) if (k in patch && patch[k] === '') { err.textContent = 'Enter a number for ' + k.replace('_', ' ') + '.'; err.classList.remove('hidden'); return; }
  setBusy(b, true);
  try {
    const r = await api('/api/config', { method: 'PUT', body: patch });
    S.config = r.config; S.draft = {}; S.secretClear = {}; drawSettings(); b.disabled = true; toast('Settings saved', 'ok');
    pollStatus();
    if (r.restart_required && await confirmSheet('Restart to apply?', 'The sniper is running with the old settings.', 'Restart now', false)) control('/api/sniper/restart', $('#btn-restart'), 'Restarting sniper');
  } catch (e) {
    err.textContent = (e.data && e.data.details ? e.data.details.join(' · ') : e.message); err.classList.remove('hidden');
  } finally { setBusy(b, false); }
};
async function editList() {
  let names; try { names = (await api('/api/customlist')).names; } catch (e) { return toast(e.message, 'err'); }
  openSheet((sh, close) => {
    sh.append(el('h3', '', 'Custom list'), el('p', '', 'One username per line. Lines starting with // are comments.'));
    const ta = el('textarea'); ta.value = names.join('\n'); ta.autocapitalize = 'off'; ta.spellcheck = false; sh.append(ta);
    const b = el('div', 'btns'), save = el('button', 'btn primary big', 'Save list'), x = el('button', 'btn ghost', 'Cancel');
    save.onclick = async () => {
      setBusy(save, true);
      try { const r = await api('/api/customlist', { method: 'PUT', body: { names: ta.value.split('\n') } }); close(); toast(r.restart_required ? 'Saved — restart to apply' : 'List saved', 'ok'); }
      catch (e) { toast(e.data && e.data.details ? e.data.details[0] : e.message, 'err'); } finally { setBusy(save, false); }
    };
    x.onclick = close; b.append(save, x); sh.append(b);
  });
}
$('#btn-forget').onclick = async () => {
  if (await confirmSheet('Disconnect?', 'Your API key will be removed from this device. The sniper keeps running on the server.', 'Disconnect', true)) { await store.del('url'); S.url = ''; signOut(); }
};

/* ---------------------------------------------------------------- boot */
$('#btn-connect').onclick = async () => { const b = $('#btn-connect'); setBusy(b, true); await connect($('#in-url').value, $('#in-key').value, true); setBusy(b, false); };
$('#in-key').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-connect').click(); });
(async () => {
  if (bridge) $('#keychain-note').textContent = ' (iOS Keychain)';
  const url = await store.get('url'), key = await store.get('key');
  const pageUrl = /^https?:$/.test(location.protocol) ? location.origin : '';
  if (url && key && await connect(url, key, false)) return;
  $('#in-url').value = url || pageUrl;
  $('#connect').classList.remove('hidden');
})();
})();
