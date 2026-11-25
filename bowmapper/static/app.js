'use strict';
/* Bow Mapper UI. Vanilla JS: live state over one WebSocket, commands over REST. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 3) => (v === null || v === undefined || Number.isNaN(+v)) ? '--' : (+v).toFixed(d);
const num = (v) => (v === '' || v === null || v === undefined || Number.isNaN(+v)) ? null : +v;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const dur = (s) => { if (s == null) return '--'; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return h ? `${h} h ${m} min` : m ? `${m} min ${x} s` : `${x} s`; };

const S = {
  m: null, l: null, cfg: null, trend: [], wsOk: false,
  droMode: 'work', gotoCoords: 'work', centerView: 'map',
  overlay: { program: null, plan: null, live: { session: null, pts: [] }, mpoints: null },
  layers: { program: true, plan: true, live: true, positions: true, die: true },
  target: null, follow: false, spots: [], selProgram: null, selSession: null, sessions: [],
  rv: { q: 'residual_um', kind: '2d', data: null },
};

/* ================================================================ api / ws */
async function api(path, body, method) {
  const opt = { method: method || (body === undefined ? 'GET' : 'POST'), headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  let r;
  try { r = await fetch(path, opt); } catch (e) { toast('error', 'Bow Mapper server not reachable. Is its window still open?'); throw e; }
  let j = {};
  try { j = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok || j.ok === false) { const msg = j.error || `${r.status} ${r.statusText}`; toast('error', msg); throw new Error(msg); }
  return j;
}
const post = (p, b = {}) => api(p, b, 'POST');

let ws = null;
function wsConnect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { S.wsOk = true; render(); };
  ws.onclose = () => { S.wsOk = false; Jog.releaseAll(); render(); setTimeout(wsConnect, 1000); };
  ws.onerror = () => { };
  ws.onmessage = (e) => { let m; try { m = JSON.parse(e.data); } catch (_) { return; } onMsg(m); };
}
function wsSend(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function onMsg(m) {
  if (m.t === 'hello') {
    S.cfg = m.config;
    $('#log').innerHTML = '';
    for (const e of m.history || []) logEvent(e, false);
    applyConfig(true);
    return;
  }
  if (m.t === 'tick') {
    S.m = m.machine; S.l = m.laser;
    if (m.trend) S.trend = m.trend;
    for (const e of m.events || []) handleEvent(e);
    render();
    return;
  }
  if (m.t === 'jog' && !m.ok) toast('warn', m.error);
}

function handleEvent(e) {
  switch (e.t) {
    case 'note': toast(e.level, e.text); logEvent(e, true); break;
    case 'log': logEvent(e, true); break;
    case 'scan_point': livePoint(e); break;
    case 'sessions_changed': loadSessions(); break;
    case 'config': S.cfg = e.config; applyConfig(false); break;
    case 'job': if (['done', 'failed', 'stopped'].includes(e.status)) { loadSessions(); if (e.kind === 'locate') loadAlign(e); } break;
    default: break;
  }
}

/* ================================================================ toast / log */
function toast(level, text) {
  if (!text) return;
  $('#sb-last').textContent = text;
  const ms = level === 'error' ? 12000 : 6000;
  // same message already showing: refresh it instead of stacking another
  const same = $$('#toasts .toast').find((t) => t.dataset.text === text);
  if (same) { clearTimeout(same._t); same._t = setTimeout(() => same.remove(), ms); return; }
  const el = document.createElement('div');
  el.className = `toast ${level || 'info'}`;
  el.dataset.text = text;
  const msg = document.createElement('span');
  msg.textContent = text;
  const x = document.createElement('button');
  x.className = 'toast-x'; x.title = 'Close'; x.textContent = '✕';
  x.onclick = () => { clearTimeout(el._t); el.remove(); };
  el.append(msg, x);
  $('#toasts').appendChild(el);
  while ($('#toasts').children.length > 5) $('#toasts').firstChild.remove();
  el._t = setTimeout(() => el.remove(), ms);
}

function logEvent(e, live) {
  const box = $('#log');
  const d = document.createElement('div');
  const time = new Date((e.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  if (e.t === 'note') { d.className = `${e.level === 'error' ? 'err' : e.level} nt`; d.textContent = `${time}  ● ${e.text}`; }
  else if (e.dir === '>') { d.className = 'tx'; d.textContent = `${time}  > ${e.line}`; }
  else { d.className = /error|ALARM/.test(e.line) ? 'err' : 'rx'; d.textContent = `${time}  < ${e.line}`; }
  applyLogFilter(d);
  box.appendChild(d);
  while (box.children.length > 2000) box.firstChild.remove();
  if ($('#lg-follow').checked) box.scrollTop = box.scrollHeight;
  if (!live) return;
}
function applyLogFilter(d) {
  const tx = $('#lg-tx').checked, rx = $('#lg-rx').checked, nt = $('#lg-notes').checked;
  const hide = (d.classList.contains('tx') && !tx) || (d.classList.contains('rx') && !rx) || (d.classList.contains('err') && !d.classList.contains('nt') && !rx) || (d.classList.contains('nt') && !nt);
  d.classList.toggle('hidden', hide);
}

/* ================================================================ render */
let lastBanner = '';
function render() {
  const m = S.m, l = S.l;
  const conn = !!(m && m.connected);
  // top bar
  const st = conn ? m.state : 'Offline';
  const p = $('#p-state');
  p.className = `pill st-${st}`;
  p.lastElementChild.textContent = conn ? (m.sub && st !== 'Alarm' ? `${st.toUpperCase()}:${m.sub}` : st.toUpperCase()) : 'OFFLINE';
  const ph = $('#p-homed');
  ph.className = `pill ${conn && m.homed ? 'yes' : 'no'}`;
  ph.textContent = conn && m.homed ? (m.homed_how === 'homing' ? 'HOMED' : `HOMED (${m.homed_how})`) : 'NOT HOMED';
  $('#c-connect').textContent = conn ? 'Disconnect' : 'Connect';
  $('#c-connect').classList.toggle('primary', !conn);
  const lr = l && l.latest;
  $('#t-laser').textContent = lr && lr.valid ? fmt(lr.distance_mm, 3) : (l && l.running ? 'out of range' : '--');
  $('#t-laser').style.color = lr && lr.valid ? '' : 'var(--muted)';
  $$('#jogpad .jb[data-d]').forEach((b) => { b.disabled = !conn; });
  // DRO
  if (m) {
    const big = S.droMode === 'work' ? m.wpos : m.mpos, small = S.droMode === 'work' ? m.mpos : m.wpos;
    ['x', 'y', 'z'].forEach((a, i) => {
      const row = $(`#dro-${a}`);
      row.querySelector('.v').innerHTML = `${fmt(big[i], 3)}<small>${S.droMode === 'work' ? 'machine' : 'work'} ${fmt(small[i], 3)}</small>`;
      const al = m.allowed && m.allowed[a];
      row.classList.toggle('atlimit', !!(conn && m.homed && al && (m.mpos[i] <= al[0] + 0.01 || m.mpos[i] >= al[1] - 0.01)));
    });
    $('#ov-feed').textContent = `${m.ov ? m.ov[0] : 100}%`;
    $('#sb-feed').textContent = `F ${Math.round(m.feed || 0)}`;
    $('#sb-ov').textContent = `Ov ${m.ov ? m.ov.join('/') : '-'}`;
    $('#sb-pins').textContent = `Pins ${m.pins || '-'}`;
    $('#sb-wco').textContent = `WCO ${m.wco.map((v) => fmt(v, 2)).join(', ')}`;
    renderProtect();
  }
  $('#sb-ws').style.color = S.wsOk ? 'var(--ok)' : 'var(--err)';
  if (m && m.accel && (!S._acc || S._acc !== m.accel.join())) { S._acc = m.accel.join(); markStep(); }
  renderBanners();
  renderLaser();
  renderJobButtons();
  Map2.request();
}

function renderProtect() {
  const m = S.m;
  const lim = m.limits || {};
  const rows = [
    ['Travel X', lim.x ? `${fmt(lim.x[0], 1)} … ${fmt(lim.x[1], 1)}` : '--'],
    ['Travel Y', lim.y ? `${fmt(lim.y[0], 1)} … ${fmt(lim.y[1], 1)}` : '--'],
    ['Travel Z', lim.z ? `${fmt(lim.z[0], 1)} … ${fmt(lim.z[1], 1)}` : '--'],
    ['Limits from', m.limits_source || '--'],
    ['Safe Z (machine)', m.safe_z == null ? 'none' : fmt(m.safe_z, 3)],
    ['Z floor (machine)', m.z_floor == null ? 'none' : fmt(m.z_floor, 3)],
  ];
  const html = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  const el = $('#protect-kv');
  if (el._h !== html) { el.innerHTML = html; el._h = html; }
}

function renderBanners() {
  const m = S.m;
  const items = [];
  if (!S.wsOk) items.push(['err', 'Lost contact with the Bow Mapper server. Reconnecting… (is its window still open?)', '']);
  else if (!m || !m.connected) items.push(['info', '<b>Not connected.</b> Pick the CNC port (usually “USB-SERIAL CH340”) and press Connect. No machine here? Choose <b>Simulator</b>.', '']);
  else {
    if (m.state === 'Alarm') {
      const a = m.alarm || {};
      items.push(['err', `<b>${a.code ? 'ALARM:' + a.code : 'LOCKED'}</b> ${esc(a.text || '')}`, '<button class="btn sm primary" data-act="home">⌂ Home</button> <button class="btn sm" data-act="unlock">Unlock</button>']);
    } else if (!m.homed) {
      items.push(['warn', '<b>Not homed.</b> Go-to, programs and scans are blocked and jogs are not limit-checked (continuous jogs stop after 25 mm).', '<button class="btn sm primary" data-act="home">⌂ Home</button> <button class="btn sm" data-act="trust" title="Only if it was homed and has not lost steps">Trust position</button>']);
    }
    if (m.state === 'Hold' && !(m.job && ['running', 'paused'].includes(m.job.status))) {
      items.push(['warn', '<b>Feed hold.</b> Motion is paused.', '<button class="btn sm primary" data-act="resume">Resume</button> <button class="btn sm danger" data-act="stop">Stop</button>']);
    }
    if (m.stale) items.push(['warn', 'No status from the controller for over 3 s. Check the cable / WiFi.', '']);
    const j = m.job;
    if (j && ['running', 'paused'].includes(j.status)) {
      const p = j.progress || {};
      const pct = p.total ? Math.round(100 * p.done / p.total) : null;
      const eta = p.eta_s != null ? ` · ETA ${dur(p.eta_s)}` : '';
      const txt = `<b>${esc(j.kind.toUpperCase())}</b> ${esc(j.name)} ${p.total ? `· ${p.done}/${p.total}${pct != null ? ` (${pct}%)` : ''}${eta}` : ''} ${j.paused ? '<b style="color:var(--warn)"> · PAUSED</b>' : ''} ${j.message ? `<br><span>${esc(j.message)}</span>` : ''}`;
      const bar = pct != null ? `<div class="progress"><div style="width:${pct}%"></div></div>` : '';
      items.push([j.paused ? 'warn' : 'info', txt + bar,
        (j.paused ? '<button class="btn sm primary" data-act="jresume">▶ Resume</button>' : '<button class="btn sm" data-act="jpause">❚❚ Pause</button>') + ' <button class="btn sm danger" data-act="stop">■ Stop</button>']);
    }
  }
  const sig = JSON.stringify(items);
  if (sig === lastBanner) return;
  lastBanner = sig;
  $('#banners').innerHTML = items.map(([lv, msg, act]) => `<div class="banner ${lv}"><div class="msg">${msg}</div><div class="row">${act}</div></div>`).join('');
}

function renderJobButtons() {
  const m = S.m;
  const conn = !!(m && m.connected);
  const busy = !!(m && m.job && ['running', 'paused'].includes(m.job.status));
  ['#g-go', '#b-gozero', '#target-go', '#r-start', '#l-standoff', '#b-raise', '#b-home'].forEach((s) => { const b = $(s); if (b) b.disabled = !conn || busy; });
  ['#b-unlock', '#b-hold', '#b-resume', '#b-floor-here', '#b-safez-here', '#pos-save'].forEach((s) => { const b = $(s); if (b) b.disabled = !conn; });
  $$('[data-zero]').forEach((b) => { b.disabled = !conn; });
}

document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  const a = b.dataset.act;
  try {
    if (a === 'home') await post('/api/home');
    else if (a === 'unlock') await post('/api/unlock');
    else if (a === 'trust') await post('/api/trust');
    else if (a === 'resume') await post('/api/resume');
    else if (a === 'stop') await post('/api/stop');
    else if (a === 'jpause') await post('/api/job/pause');
    else if (a === 'jresume') await post('/api/job/resume');
  } catch (_) { /* toast shown */ }
});

/* ================================================================ jog */
const Jog = {
  step: 1, hold: false, speed: 'normal', keys: true, active: null, held: new Set(),
  dirToMachine(d) {
    const fx = S.cfg && S.cfg.ui.flip_x ? -1 : 1, fy = S.cfg && S.cfg.ui.flip_y ? -1 : 1;
    return [d[0] * fx, d[1] * fy, d[2]];
  },
  press(d, src, forceCont) {
    if (!S.m || !S.m.connected) { toast('warn', 'Connect to the CNC first.'); return; }
    this.release();
    const md = this.dirToMachine(d);
    const a = { d: md, src, cont: false, timer: null, keep: null };
    this.active = a;
    if (this.hold || forceCont) { this.startCont(a); }
    else {
      wsSend({ op: 'jog_step', x: md[0] * this.step, y: md[1] * this.step, z: md[2] * this.step, speed: this.speed });
      if (this.step <= 1) a.timer = setTimeout(() => { if (this.active === a) this.startCont(a); }, 420);
    }
  },
  startCont(a) {
    a.cont = true;
    wsSend({ op: 'jog_start', x: a.d[0], y: a.d[1], z: a.d[2], speed: this.speed });
    a.keep = setInterval(() => wsSend({ op: 'jog_keep' }), 150);
  },
  release(src) {
    const a = this.active;
    if (!a || (src && a.src !== src)) return;
    clearTimeout(a.timer); clearInterval(a.keep);
    if (a.cont) wsSend({ op: 'jog_stop' });
    this.active = null;
    $$('#jogpad .jb.active').forEach((b) => b.classList.remove('active'));
  },
  releaseAll() { this.held.clear(); this.release(); },
};

function renderSteps() {
  const steps = (S.cfg && S.cfg.jog.steps) || [0.01, 0.1, 1, 10, 50];
  $('#steps').innerHTML = steps.map((s, i) => `<button data-step="${s}" title="Key ${i + 1}">${s}</button>`).join('') + '<button data-step="hold" title="Key 0: move while held">Hold ∞</button>';
  markStep();
}
function markStep() {
  $$('#steps button').forEach((b) => b.classList.toggle('on', Jog.hold ? b.dataset.step === 'hold' : +b.dataset.step === Jog.step));
  const j = S.cfg ? S.cfg.jog : null;
  if (j) {
    const f = { fine: j.fine_factor, fast: j.fast_factor }[Jog.speed] || 1;
    const fxy = j.feed_xy * f, fz = j.feed_z * f;
    const acc = (S.m && S.m.accel) || [50, 50, 50];
    const coast = (v, a) => ((v / 60) ** 2 / (2 * a));
    $('#jog-feed').innerHTML = `<span class="mono">${Math.round(fxy)}</span><small>XY mm/min<br>Z ${Math.round(fz)}</small>`;
    $('#jog-feed').title = `Held jogs coast to a stop after release: about ${coast(fxy, acc[0]).toFixed(1)} mm in XY and ${coast(fz, acc[2]).toFixed(2)} mm in Z at this speed (acceleration $120-$122). Steps stop exactly.`;
    $('#jog-coast').textContent = `At this speed a held jog coasts ~${coast(fxy, acc[0]).toFixed(1)} mm (XY) / ${coast(fz, acc[2]).toFixed(2)} mm (Z) after release. Use Fine near the part; steps stop exactly.`;
  }
}
$('#steps').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.step === 'hold') Jog.hold = true; else { Jog.hold = false; Jog.step = +b.dataset.step; }
  markStep();
});
$('#speed').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  Jog.speed = b.dataset.v; $$('#speed button').forEach((x) => x.classList.toggle('on', x === b)); markStep();
});
$$('#jogpad .jb[data-d]').forEach((b) => {
  const d = b.dataset.d.split(',').map(Number);
  b.addEventListener('pointerdown', (e) => {
    if (b.disabled || e.button !== 0) return;
    e.preventDefault(); b.setPointerCapture(e.pointerId); b.classList.add('active');
    Jog.press(d, 'pad');
  });
  const up = () => { b.classList.remove('active'); Jog.release('pad'); };
  b.addEventListener('pointerup', up); b.addEventListener('pointercancel', up); b.addEventListener('lostpointercapture', up);
  b.addEventListener('contextmenu', (e) => e.preventDefault());
});
$('#jog-stop').addEventListener('click', () => { Jog.releaseAll(); wsSend({ op: 'jog_stop' }); });
$('#b-kbd').addEventListener('click', () => {
  Jog.keys = !Jog.keys; $('#b-kbd').classList.toggle('on', Jog.keys); $('#b-kbd').textContent = Jog.keys ? '⌨ Keys on' : '⌨ Keys off';
  if (!Jog.keys) Jog.releaseAll();
});

const KEYDIR = { ArrowRight: [1, 0, 0], ArrowLeft: [-1, 0, 0], ArrowUp: [0, 1, 0], ArrowDown: [0, -1, 0], PageUp: [0, 0, 1], PageDown: [0, 0, -1] };
function typing(e) { const t = e.target; return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable); }
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.preventDefault(); Jog.releaseAll(); post('/api/stop').catch(() => { }); return; }
  if (typing(e) || !Jog.keys) return;
  if (KEYDIR[e.key]) {
    e.preventDefault();
    if (e.repeat) return;
    Jog.held.add(e.key);
    const d = [0, 0, 0];
    for (const k of Jog.held) KEYDIR[k].forEach((v, i) => { d[i] += v; });
    if (!d.some((v) => v)) { Jog.release(); return; }
    Jog.press(d.map((v) => Math.sign(v)), 'key', e.shiftKey || Jog.held.size > 1);
    return;
  }
  const steps = (S.cfg && S.cfg.jog.steps) || [];
  if (/^[1-9]$/.test(e.key) && steps[+e.key - 1] !== undefined) { Jog.hold = false; Jog.step = steps[+e.key - 1]; markStep(); }
  else if (e.key === '0') { Jog.hold = true; markStep(); }
});
document.addEventListener('keyup', (e) => {
  if (KEYDIR[e.key]) { Jog.held.delete(e.key); Jog.release('key'); }
});
window.addEventListener('blur', () => Jog.releaseAll());
document.addEventListener('visibilitychange', () => { if (document.hidden) Jog.releaseAll(); });

function relabelPad() {
  const fx = S.cfg && S.cfg.ui.flip_x, fy = S.cfg && S.cfg.ui.flip_y;
  const set = (k, t) => { const el = $(`[data-lbl="${k}"]`); if (el) el.textContent = t; };
  set('x+', fx ? 'X−' : 'X+'); set('x-', fx ? 'X+' : 'X−'); set('y+', fy ? 'Y−' : 'Y+'); set('y-', fy ? 'Y+' : 'Y−');
}

/* ================================================================ bed map */
const Map2 = {
  cv: $('#map'), ctx: null, scale: 1, ox: 0, oy: 0, dpr: 1, need: false, fitted: false, drag: null,
  init() {
    this.ctx = this.cv.getContext('2d');
    new ResizeObserver(() => { this.resize(); }).observe($('#mapwrap'));
    this.cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = this.cv.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
      const [mx, my] = this.toMach(px, py);
      this.scale = Math.max(0.3, Math.min(400, this.scale * Math.pow(1.0018, -e.deltaY)));
      const [sx, sy] = this.toScr(mx, my);
      this.ox += px - sx; this.oy += py - sy; this.request();
    }, { passive: false });
    this.cv.addEventListener('pointerdown', (e) => { this.drag = { x: e.clientX, y: e.clientY, ox: this.ox, oy: this.oy, moved: false }; this.cv.setPointerCapture(e.pointerId); });
    this.cv.addEventListener('pointermove', (e) => {
      const r = this.cv.getBoundingClientRect();
      const [mx, my] = this.toMach(e.clientX - r.left, e.clientY - r.top);
      const w = S.m ? [mx - S.m.wco[0], my - S.m.wco[1]] : [mx, my];
      $('#hover').textContent = `Machine X ${fmt(mx, 2)} Y ${fmt(my, 2)}   ·   Work X ${fmt(w[0], 2)} Y ${fmt(w[1], 2)}`;
      if (this.drag) {
        const dx = e.clientX - this.drag.x, dy = e.clientY - this.drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) this.drag.moved = true;
        if (this.drag.moved) { this.ox = this.drag.ox + dx; this.oy = this.drag.oy + dy; S.follow = false; $('#m-follow').classList.remove('on'); this.request(); }
      }
    });
    this.cv.addEventListener('pointerup', (e) => {
      const d = this.drag; this.drag = null;
      if (d && !d.moved) {
        const r = this.cv.getBoundingClientRect();
        const [mx, my] = this.toMach(e.clientX - r.left, e.clientY - r.top);
        setTarget(mx, my);
      }
    });
    this.cv.addEventListener('dblclick', () => { if (S.target) goTarget(); });
    this.resize();
  },
  resize() {
    const r = $('#mapwrap').getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    this.cv.width = Math.max(1, r.width * this.dpr); this.cv.height = Math.max(1, r.height * this.dpr);
    this.w = r.width; this.h = r.height;
    if (!this.fitted && S.m) { this.fitBed(); this.fitted = true; }
    this.request();
  },
  fx() { return S.cfg && S.cfg.ui.flip_x ? -1 : 1; },
  fy() { return S.cfg && S.cfg.ui.flip_y ? -1 : 1; },
  toScr(x, y) { return [this.ox + this.fx() * x * this.scale, this.oy - this.fy() * y * this.scale]; },
  toMach(px, py) { return [(px - this.ox) / (this.scale * this.fx()), (this.oy - py) / (this.scale * this.fy())]; },
  fit(x0, y0, x1, y1) {
    if (!this.w) return;
    const pad = 30, w = Math.max(x1 - x0, 1), h = Math.max(y1 - y0, 1);
    this.scale = Math.min((this.w - 2 * pad) / w, (this.h - 2 * pad) / h);
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    this.ox = this.w / 2 - this.fx() * cx * this.scale; this.oy = this.h / 2 + this.fy() * cy * this.scale;
    this.request();
  },
  fitBed() { const l = S.m && S.m.limits; if (l) this.fit(l.x[0], l.y[0], l.x[1], l.y[1]); else this.fit(0, 0, 400, 400); },
  fitJob() {
    const xs = [], ys = [];
    const o = S.overlay;
    if (o.plan && S.layers.plan) o.plan.pts.forEach((p) => { xs.push(p[2]); ys.push(p[3]); });
    if (o.program && S.layers.program) o.program.segs.forEach((s) => { xs.push(s[0], s[2]); ys.push(s[1], s[3]); });
    if (o.live.pts.length) o.live.pts.forEach((p) => { xs.push(p.xm); ys.push(p.ym); });
    if (o.mpoints) o.mpoints.forEach((p) => { xs.push(p[0]); ys.push(p[1]); });
    if (!xs.length) { toast('info', 'Nothing to fit: preview a scan or a program first.'); return; }
    const m = 3;
    this.fit(Math.min(...xs) - m, Math.min(...ys) - m, Math.max(...xs) + m, Math.max(...ys) + m);
  },
  request() { if (this.need) return; this.need = true; requestAnimationFrame(() => this.draw()); },
  draw() {
    this.need = false;
    if (S.centerView !== 'map') return;
    const c = this.ctx, d = this.dpr;
    c.setTransform(d, 0, 0, d, 0, 0);
    c.fillStyle = css('--map-bg'); c.fillRect(0, 0, this.w, this.h);
    const m = S.m;
    const lim = (m && m.limits) || { x: [0, 400], y: [0, 400], z: [-130, 0] };
    if (S.follow && m) {
      const [hx, hy] = this.toScr(m.mpos[0], m.mpos[1]);
      if (hx < 60 || hy < 60 || hx > this.w - 60 || hy > this.h - 60) { this.ox += this.w / 2 - hx; this.oy += this.h / 2 - hy; }
    }
    // bed
    const [bx0, by0] = this.toScr(lim.x[0], lim.y[0]), [bx1, by1] = this.toScr(lim.x[1], lim.y[1]);
    c.fillStyle = css('--map-bed');
    c.fillRect(Math.min(bx0, bx1), Math.min(by0, by1), Math.abs(bx1 - bx0), Math.abs(by1 - by0));
    // grid
    const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100];
    const minor = steps.find((s) => s * this.scale >= 14) || 100;
    const major = steps.find((s) => s >= minor * 5) || minor * 5;
    const [vx0, vy0] = this.toMach(0, this.h), [vx1, vy1] = this.toMach(this.w, 0);
    const gx0 = Math.max(lim.x[0], Math.min(vx0, vx1)), gx1 = Math.min(lim.x[1], Math.max(vx0, vx1));
    const gy0 = Math.max(lim.y[0], Math.min(vy0, vy1)), gy1 = Math.min(lim.y[1], Math.max(vy0, vy1));
    c.lineWidth = 1;
    for (const [step, col] of [[minor, css('--map-grid')], [major, css('--map-grid-major')]]) {
      c.strokeStyle = col; c.beginPath();
      for (let x = Math.ceil(gx0 / step) * step; x <= gx1; x += step) { const [sx] = this.toScr(x, 0); c.moveTo(Math.round(sx) + .5, Math.min(by0, by1)); c.lineTo(Math.round(sx) + .5, Math.max(by0, by1)); }
      for (let y = Math.ceil(gy0 / step) * step; y <= gy1; y += step) { const [, sy] = this.toScr(0, y); c.moveTo(Math.min(bx0, bx1), Math.round(sy) + .5); c.lineTo(Math.max(bx0, bx1), Math.round(sy) + .5); }
      c.stroke();
    }
    c.fillStyle = css('--faint'); c.font = `10px ${css('--mono')}`;
    for (let x = Math.ceil(gx0 / major) * major; x <= gx1; x += major) { const [sx] = this.toScr(x, 0); c.fillText(`${+x.toFixed(2)}`, sx + 3, Math.max(by0, by1) - 4); }
    for (let y = Math.ceil(gy0 / major) * major; y <= gy1; y += major) { const [, sy] = this.toScr(0, y); c.fillText(`${+y.toFixed(2)}`, Math.min(bx0, bx1) + 3, sy - 3); }
    // travel + allowed
    c.strokeStyle = css('--line-2'); c.lineWidth = 1.5;
    c.strokeRect(Math.min(bx0, bx1), Math.min(by0, by1), Math.abs(bx1 - bx0), Math.abs(by1 - by0));
    if (m && m.allowed) {
      const al = m.allowed, [ax0, ay0] = this.toScr(al.x[0], al.y[0]), [ax1, ay1] = this.toScr(al.x[1], al.y[1]);
      c.setLineDash([4, 4]); c.strokeStyle = m.homed ? css('--muted') : css('--warn');
      c.strokeRect(Math.min(ax0, ax1), Math.min(ay0, ay1), Math.abs(ax1 - ax0), Math.abs(ay1 - ay0)); c.setLineDash([]);
    }
    c.fillStyle = css('--muted'); c.font = `11px ${css('--sans')}`;
    c.fillText(this.fx() > 0 ? 'X+ →' : '← X+', Math.max(bx0, bx1) - 40, Math.max(by0, by1) + 14);
    c.fillText(this.fy() > 0 ? 'Y+ ↑' : 'Y+ ↓', Math.min(bx0, bx1) - 34, Math.min(by0, by1) + 12);
    // work origin
    if (m) {
      const [wx, wy] = this.toScr(m.wco[0], m.wco[1]);
      c.strokeStyle = css('--ok'); c.lineWidth = 1.5; c.beginPath();
      c.moveTo(wx - 10, wy); c.lineTo(wx + 10, wy); c.moveTo(wx, wy - 10); c.lineTo(wx, wy + 10); c.stroke();
      c.beginPath(); c.arc(wx, wy, 4, 0, Math.PI * 2); c.stroke();
      c.fillStyle = css('--ok'); c.font = `10.5px ${css('--sans')}`; c.fillText('work 0', wx + 6, wy - 6);
    }
    // positions
    if (S.layers.positions && S.cfg) {
      c.font = `10.5px ${css('--sans')}`;
      for (const p of S.cfg.ui.positions || []) {
        const [px, py] = this.toScr(p.x, p.y);
        c.fillStyle = css('--warn'); c.beginPath(); c.moveTo(px, py - 5); c.lineTo(px + 5, py); c.lineTo(px, py + 5); c.lineTo(px - 5, py); c.closePath(); c.fill();
        c.fillText(p.name, px + 7, py + 3);
      }
    }
    // found die outline (Find silicon)
    const al = S.cfg && S.cfg.align;
    if (S.layers.die && al && al.corners_machine) {
      c.save(); c.strokeStyle = css('--ok'); c.lineWidth = 2; c.setLineDash([]);
      c.fillStyle = css('--ok-soft'); c.beginPath();
      al.corners_machine.forEach((p, i) => { const [sx, sy] = this.toScr(p[0], p[1]); if (i) c.lineTo(sx, sy); else c.moveTo(sx, sy); });
      c.closePath(); c.fill(); c.stroke();
      al.corners_machine.forEach((p, i) => { const [sx, sy] = this.toScr(p[0], p[1]); c.fillStyle = css('--ok'); c.beginPath(); c.arc(sx, sy, 3.5, 0, Math.PI * 2); c.fill(); c.font = `10.5px ${css('--sans')}`; c.fillText(String(i + 1), sx + 5, sy - 5); });
      const [ccx, ccy] = this.toScr(al.center_machine[0], al.center_machine[1]);
      c.beginPath(); c.moveTo(ccx - 7, ccy); c.lineTo(ccx + 7, ccy); c.moveTo(ccx, ccy - 7); c.lineTo(ccx, ccy + 7); c.stroke();
      const [tx, ty] = this.toScr(al.corners_machine[3][0], al.corners_machine[3][1]);
      c.font = `600 11.5px ${css('--sans')}`; c.fillStyle = css('--ok');
      c.fillText(`Die ${fmt(al.width, 2)} × ${fmt(al.height, 2)} mm  ${fmt(al.angle_deg, 2)}°`, tx, ty - 10);
      c.restore();
    }
    // corners entered by eye (not yet used)
    if (S.corners) {
      c.save(); c.strokeStyle = css('--warn'); c.fillStyle = css('--warn'); c.lineWidth = 2; c.font = `600 11px ${css('--sans')}`;
      for (const k of CORNER_KEYS) {
        const p = S.corners[k]; if (!p) continue;
        const [sx, sy] = this.toScr(p[0], p[1]);
        c.beginPath(); c.moveTo(sx - 7, sy - 7); c.lineTo(sx + 7, sy + 7); c.moveTo(sx + 7, sy - 7); c.lineTo(sx - 7, sy + 7); c.stroke();
        c.fillText(CORNER_LABEL[k], sx + 8, sy - 8);
      }
      c.restore();
    }
    // program preview
    const o = S.overlay;
    if (S.layers.program && o.program) {
      c.lineWidth = 1;
      for (const kind of [0, 1]) {
        c.beginPath(); c.strokeStyle = kind ? css('--accent') : css('--faint'); c.setLineDash(kind ? [] : [3, 3]);
        for (const s of o.program.segs) { if ((s[4] ? 1 : 0) !== kind) continue; const [a, b] = this.toScr(s[0], s[1]); const [e, f] = this.toScr(s[2], s[3]); c.moveTo(a, b); c.lineTo(e, f); }
        c.stroke();
      }
      c.setLineDash([]);
    }
    if (S.layers.program && o.mpoints) {
      c.fillStyle = css('--accent');
      for (const p of o.mpoints) { const [px, py] = this.toScr(p[0], p[1]); c.beginPath(); c.arc(px, py, 3, 0, Math.PI * 2); c.fill(); }
    }
    // scan plan
    if (S.layers.plan && o.plan && m) {
      c.strokeStyle = css('--accent'); c.lineWidth = 1.2;
      for (const ol of o.plan.outlines || []) {
        c.beginPath();
        ol.pts.forEach((p, i) => { const [sx, sy] = this.toScr(p[0], p[1]); if (i) c.lineTo(sx, sy); else c.moveTo(sx, sy); });
        c.closePath(); c.stroke();
        const [lx, ly] = this.toScr(ol.pts[3][0], ol.pts[3][1]);
        c.fillStyle = css('--accent'); c.font = `10.5px ${css('--sans')}`; c.fillText(ol.name, lx + 3, ly - 4);
      }
      const rad = Math.max(1, Math.min(3, this.scale * 0.15));
      c.strokeStyle = css('--muted');
      if (o.plan.pts.length < 30000) {
        c.beginPath();
        for (const p of o.plan.pts) { const [px, py] = this.toScr(p[2], p[3]); c.moveTo(px + rad, py); c.arc(px, py, rad, 0, Math.PI * 2); }
        c.stroke();
      }
      if (o.plan.errors && o.plan.errors.length) { c.fillStyle = css('--err'); c.fillText('Outside allowed travel!', 12, 18); }
    }
    // live measured
    if (S.layers.live && o.live.pts.length) {
      const vals = o.live.pts.map((p) => p.v).filter((v) => v != null);
      const lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 1;
      const sz = Math.max(3, Math.min(18, (o.live.pitch || 1) * this.scale * 0.92));
      for (const p of o.live.pts) {
        const [px, py] = this.toScr(p.xm, p.ym);
        c.fillStyle = p.v == null ? css('--err') : viridis(hi > lo ? (p.v - lo) / (hi - lo) : 0.5);
        c.fillRect(px - sz / 2, py - sz / 2, sz, sz);
      }
      showLegend(lo, hi, vals.length, o.live.unit || 'µm');
    } else { $('#legend').classList.add('hidden'); }
    // target
    if (S.target) {
      const [tx, ty] = this.toScr(S.target[0], S.target[1]);
      c.strokeStyle = css('--accent'); c.lineWidth = 2; c.beginPath();
      c.moveTo(tx - 9, ty - 9); c.lineTo(tx + 9, ty + 9); c.moveTo(tx + 9, ty - 9); c.lineTo(tx - 9, ty + 9); c.stroke();
    }
    // head
    if (m) {
      const [hx, hy] = this.toScr(m.mpos[0], m.mpos[1]);
      const col = { Idle: css('--ok'), Run: css('--accent'), Jog: css('--accent'), Home: css('--accent'), Alarm: css('--err'), Hold: css('--warn') }[m.state] || css('--muted');
      c.strokeStyle = col; c.globalAlpha = .35; c.lineWidth = 1; c.beginPath();
      c.moveTo(0, hy); c.lineTo(this.w, hy); c.moveTo(hx, 0); c.lineTo(hx, this.h); c.stroke(); c.globalAlpha = 1;
      c.lineWidth = 2.5; c.beginPath(); c.arc(hx, hy, 8, 0, Math.PI * 2); c.stroke();
      c.fillStyle = col; c.beginPath(); c.arc(hx, hy, 2.5, 0, Math.PI * 2); c.fill();
    }
    // scale bar
    const sb = steps.find((s) => s * this.scale >= 60) || 100;
    c.strokeStyle = css('--fg'); c.lineWidth = 2; c.beginPath(); c.moveTo(16, this.h - 40); c.lineTo(16 + sb * this.scale, this.h - 40); c.stroke();
    c.fillStyle = css('--fg'); c.font = `11px ${css('--mono')}`; c.fillText(`${sb} mm`, 16, this.h - 46);
  },
};

function viridis(t) {
  t = Math.max(0, Math.min(1, t));
  const C = [[68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142], [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37]];
  const x = t * (C.length - 1), i = Math.min(C.length - 2, Math.floor(x)), f = x - i;
  const c = C[i].map((v, k) => Math.round(v + (C[i + 1][k] - v) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
function showLegend(lo, hi, n, unit) {
  $('#legend').classList.remove('hidden');
  const cv = $('#lg-bar'), c = cv.getContext('2d');
  for (let i = 0; i < cv.width; i++) { c.fillStyle = viridis(i / (cv.width - 1)); c.fillRect(i, 0, 1, cv.height); }
  $('#lg-lo').textContent = `${fmt(lo, 1)} ${unit}`; $('#lg-hi').textContent = `${fmt(hi, 1)} ${unit}`;
  $('#lg-n').textContent = `${n} pts`; $('#lg-title').textContent = S.overlay.live.title || 'Height';
}

function setTarget(mx, my) {
  S.target = [mx, my];
  const w = S.m ? [mx - S.m.wco[0], my - S.m.wco[1]] : [mx, my];
  $('#targetchip').classList.remove('hidden');
  $('#target-txt').textContent = `Work X ${fmt(w[0], 2)}  Y ${fmt(w[1], 2)}`;
  if (S.gotoCoords === 'work') { $('#g-x').value = w[0].toFixed(3); $('#g-y').value = w[1].toFixed(3); }
  else { $('#g-x').value = mx.toFixed(3); $('#g-y').value = my.toFixed(3); }
  Map2.request();
}
async function goTarget() {
  if (!S.target) return;
  try { await post('/api/goto', { x: S.target[0], y: S.target[1], coords: 'machine', safe_z: $('#g-safe').checked }); } catch (_) { }
}
$('#target-go').addEventListener('click', goTarget);
$('#target-x').addEventListener('click', () => { S.target = null; $('#targetchip').classList.add('hidden'); Map2.request(); });
$('#m-fitbed').addEventListener('click', () => Map2.fitBed());
$('#m-fitjob').addEventListener('click', () => {
  const al = S.cfg && S.cfg.align;
  if (al && al.corners_machine && !S.overlay.plan && !S.overlay.program && !S.overlay.live.pts.length) {
    const xs = al.corners_machine.map((p) => p[0]), ys = al.corners_machine.map((p) => p[1]);
    Map2.fit(Math.min(...xs) - 8, Math.min(...ys) - 8, Math.max(...xs) + 8, Math.max(...ys) + 8); return;
  }
  Map2.fitJob();
});
$('#m-follow').addEventListener('click', () => { S.follow = !S.follow; $('#m-follow').classList.toggle('on', S.follow); Map2.request(); });
$('#m-clear').addEventListener('click', () => { S.overlay.program = null; S.overlay.plan = null; S.overlay.mpoints = null; S.overlay.live = { session: null, pts: [] }; Map2.request(); });
$$('[data-layer]').forEach((cb) => cb.addEventListener('change', () => { S.layers[cb.dataset.layer] = cb.checked; Map2.request(); }));

function livePoint(e) {
  const L = S.overlay.live;
  if (L.session !== e.session) {
    const isScan = e.h_um !== null && e.h_um !== undefined || e.label === undefined;
    S.overlay.live = { session: e.session, pts: [], pitch: isScan && S.overlay.plan ? S.overlay.plan.pitch : 0.6, title: 'Height vs first point', unit: 'µm', ref: null };
  }
  const LL = S.overlay.live;
  let v = null;
  if (e.h_um != null) v = e.h_um;
  else if (e.distance_mm != null) { if (LL.ref == null) LL.ref = e.distance_mm; v = -(e.distance_mm - LL.ref) * 1000; }
  LL.pts.push({ xm: e.xm, ym: e.ym, v: e.flag === 'out-of-range' ? null : v });
  Map2.request();
}

/* ================================================================ left panel actions */
$('#dro-mode').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; S.droMode = b.dataset.v; $$('#dro-mode button').forEach((x) => x.classList.toggle('on', x === b)); render(); });
$('#goto-coords').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; S.gotoCoords = b.dataset.v; $$('#goto-coords button').forEach((x) => x.classList.toggle('on', x === b)); });
$$('[data-zero]').forEach((b) => b.addEventListener('click', () => post('/api/zero', { axes: b.dataset.zero }).catch(() => { })));
$('#b-gozero').addEventListener('click', () => post('/api/goto', { x: 0, y: 0, coords: 'work', safe_z: true }).catch(() => { }));
$('#g-go').addEventListener('click', () => post('/api/goto', { x: num($('#g-x').value), y: num($('#g-y').value), z: num($('#g-z').value), coords: S.gotoCoords, safe_z: $('#g-safe').checked }).catch(() => { }));
$('#g-here').addEventListener('click', () => { if (!S.m) return; const p = S.gotoCoords === 'work' ? S.m.wpos : S.m.mpos; ['x', 'y', 'z'].forEach((a, i) => { $(`#g-${a}`).value = p[i].toFixed(3); }); });
['#g-x', '#g-y', '#g-z'].forEach((s) => $(s).addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#g-go').click(); }));
$('#b-home').addEventListener('click', () => post('/api/home').catch(() => { }));
$('#b-unlock').addEventListener('click', () => post('/api/unlock').catch(() => { }));
$('#b-hold').addEventListener('click', () => post('/api/hold').catch(() => { }));
$('#b-resume').addEventListener('click', () => post('/api/resume').catch(() => { }));
$('#stopbtn').addEventListener('click', () => { Jog.releaseAll(); post('/api/stop').catch(() => { }); });
$('#estopbtn').addEventListener('click', () => { Jog.releaseAll(); post('/api/estop').catch(() => { }); });
$('#b-safez-here').addEventListener('click', () => post('/api/safez', { action: 'here' }).catch(() => { }));
$('#b-raise').addEventListener('click', () => { if (S.m && S.m.safe_z != null) post('/api/goto', { z: S.m.safe_z, coords: 'machine', safe_z: false }).catch(() => { }); });
$('#b-floor-here').addEventListener('click', () => post('/api/zfloor', { action: 'here' }).catch(() => { }));
$('#b-floor-clear').addEventListener('click', () => post('/api/zfloor', { action: 'clear' }).catch(() => { }));
$$('[data-ov]').forEach((b) => b.addEventListener('click', () => post('/api/override', { kind: 'feed', action: b.dataset.ov }).catch(() => { })));
$('#pos-save').addEventListener('click', async () => {
  const name = $('#pos-name').value.trim();
  if (!name) { toast('warn', 'Type a name for the position first.'); $('#pos-name').focus(); return; }
  try { await post('/api/positions', { action: 'save', name }); $('#pos-name').value = ''; } catch (_) { }
});
$('#pos-name').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#pos-save').click(); });
function renderPositions() {
  const list = (S.cfg && S.cfg.ui.positions) || [];
  $('#plist').innerHTML = list.length ? list.map((p) => `<div class="prow"><span class="nm" title="machine ${fmt(p.x, 2)}, ${fmt(p.y, 2)}, ${fmt(p.z, 2)}">${esc(p.name)} <span class="muted mono small">${fmt(p.x, 1)}, ${fmt(p.y, 1)}, ${fmt(p.z, 1)}</span></span><button class="btn sm" data-pgo="${esc(p.name)}">Go</button><button class="btn sm ghost" data-pdel="${esc(p.name)}">✕</button></div>`).join('') : '<div class="muted small">None yet. Saved in machine coordinates (survive a change of work zero).</div>';
}
$('#plist').addEventListener('click', (e) => {
  const g = e.target.closest('[data-pgo]'), d = e.target.closest('[data-pdel]');
  if (g) post('/api/positions', { action: 'goto', name: g.dataset.pgo }).catch(() => { });
  if (d) post('/api/positions', { action: 'delete', name: d.dataset.pdel }).catch(() => { });
});

/* ================================================================ connection */
async function loadPorts() {
  try {
    const j = await api('/api/ports');
    const cur = S.cfg && S.cfg.machine.port;
    const sel = $('#c-port');
    sel.innerHTML = j.ports.length ? j.ports.map((p) => `<option value="${esc(p.device)}"${p.device === cur ? ' selected' : ''}>${esc(p.device)} · ${esc(p.description.replace(/\s*\(COM\d+\)/, ''))}${p.guess === 'cnc' ? ' (CNC?)' : p.guess === 'laser' ? ' (laser?)' : ''}</option>`).join('') : '<option value="">No serial ports</option>';
    if (!cur) { const g = j.ports.find((p) => p.guess === 'cnc'); if (g) sel.value = g.device; }
  } catch (_) { }
}
function connUI() {
  const t = $('#c-transport').value;
  $('#c-port').classList.toggle('hidden', t !== 'serial'); $('#c-refresh').classList.toggle('hidden', t !== 'serial');
  $('#c-host').classList.toggle('hidden', t !== 'tcp');
}
$('#c-transport').addEventListener('change', connUI);
$('#c-refresh').addEventListener('click', loadPorts);
$('#c-connect').addEventListener('click', async () => {
  if (S.m && S.m.connected) { await post('/api/disconnect').catch(() => { }); return; }
  const t = $('#c-transport').value;
  const b = $('#c-connect'); b.disabled = true; b.textContent = 'Connecting…';
  try {
    await post('/api/connect', { transport: t, port: $('#c-port').value || null, host: $('#c-host').value || null });
    Map2.fitted = false; setTimeout(() => { Map2.fitBed(); Map2.fitted = true; }, 300);
    if (t === 'sim' && !(S.l && S.l.running)) toast('info', 'Simulator connected. For a simulated laser too: Laser tab → Simulated → Connect.');
  } catch (_) { } finally { b.disabled = false; render(); }
});

/* ================================================================ laser tab */
let lDriver = 'medaq';
$('#l-driver').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; lDriver = b.dataset.v; $$('#l-driver button').forEach((x) => x.classList.toggle('on', x === b)); $('#l-port').classList.toggle('hidden', lDriver !== 'medaq'); });
$('#l-connect').addEventListener('click', () => post('/api/laser/connect', { driver: lDriver, port: $('#l-port').value || undefined }).catch(() => { }));
$('#l-disconnect').addEventListener('click', () => post('/api/laser/disconnect').catch(() => { }));
$('#l-standoff').addEventListener('click', () => post('/api/laser/standoff').catch(() => { }));
$('#m-go').addEventListener('click', async () => {
  const b = $('#m-go'); b.disabled = true;
  try {
    const j = await post('/api/laser/measure', { samples: +$('#m-n').value || 100, label: $('#m-label').value });
    S.spots.push({ ...j.row, std_um: j.stats.std_um, valid: j.stats.valid });
    renderSpots();
    if (!j.stats.valid) toast('warn', 'No valid laser readings (out of range?).');
  } catch (_) { } finally { b.disabled = false; }
});
function renderSpots() {
  $('#spots tbody').innerHTML = S.spots.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.label || '')}</td><td class="n">${fmt(r.x_work, 3)}</td><td class="n">${fmt(r.y_work, 3)}</td><td class="n">${fmt(r.distance_mm, 4)}</td><td class="n">${fmt(r.height_mm, 4)}</td><td class="n">${fmt(r.std_um, 2)}</td></tr>`).join('');
  const tw = $('#spots').parentElement; tw.scrollTop = tw.scrollHeight;
}
$('#spots-clear').addEventListener('click', () => { S.spots = []; renderSpots(); });
$('#spots-copy').addEventListener('click', async () => {
  const head = 'n,label,x_work,y_work,z_work,x_mach,y_mach,z_mach,distance_mm,height_mm,std_um,time';
  const txt = [head, ...S.spots.map((r, i) => [i + 1, r.label, r.x_work, r.y_work, r.z_work, r.x_mach, r.y_mach, r.z_mach, r.distance_mm, r.height_mm, r.std_um, r.time].join(','))].join('\n');
  try { await navigator.clipboard.writeText(txt); toast('ok', `Copied ${S.spots.length} rows.`); } catch (_) { toast('warn', 'Clipboard blocked by the browser.'); }
});

function renderLaser() {
  const l = S.l;
  if (!l) return;
  const badge = $('#l-badge');
  const lr = l.latest;
  if (!l.running) { badge.className = 'badge mut'; badge.textContent = 'OFF'; }
  else if (!l.connected) { badge.className = 'badge err'; badge.textContent = 'NOT CONNECTED'; }
  else if (lr && lr.valid) { badge.className = 'badge ok'; badge.textContent = `IN RANGE · ${l.rate_hz} Hz`; }
  else { badge.className = 'badge warn'; badge.textContent = 'OUT OF RANGE'; }
  $('#l-dist').innerHTML = `${lr && lr.valid ? fmt(lr.distance_mm, 4) : '--'}<small> mm</small>`;
  $('#l-meta').innerHTML = lr && lr.valid ? `reading ${fmt(lr.reading, 4)} (${l.mode === 'smr' ? '0–' + l.range_mm : l.smr_mm + '–' + (l.smr_mm + l.range_mm)})<br>gap to near end ${fmt(lr.gap_mm, 2)} mm` : (l.error ? esc(l.error).slice(0, 140) : (l.running ? 'no valid reading' : 'sensor off'));
  $('#l-mk').style.left = `${lr && lr.valid ? Math.max(0, Math.min(100, lr.pos_pct)) : 50}%`;
  $('#l-mk').style.opacity = lr && lr.valid ? 1 : .25;
  $('#l-near').textContent = `${l.smr_mm} near`; $('#l-mid').textContent = `${l.mid_mm} mid`; $('#l-far').textContent = `${l.smr_mm + l.range_mm} far`;
  drawTrend();
}
function drawTrend() {
  const cv = $('#trend'), c = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  const pts = (S.trend || []).filter((p) => p[1] != null);
  if (pts.length < 2 || !(S.l && S.l.running)) { c.fillStyle = css('--faint'); c.font = `11px ${css('--sans')}`; c.fillText('No data', 10, 20); return; }
  const mean = pts.reduce((a, p) => a + p[1], 0) / pts.length;
  const vs = pts.map((p) => (p[1] - mean) * 1000);
  const sd = Math.sqrt(vs.reduce((a, v) => a + v * v, 0) / vs.length);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (hi - lo < 2) { const m = (hi + lo) / 2; lo = m - 1; hi = m + 1; }
  const x = (t) => 6 + (t + 10) / 10 * (W - 12), y = (v) => H - 14 - (v - lo) / (hi - lo) * (H - 28);
  c.strokeStyle = css('--line'); c.beginPath(); c.moveTo(0, y(0)); c.lineTo(W, y(0)); c.stroke();
  c.strokeStyle = css('--accent'); c.lineWidth = 1.4; c.beginPath();
  pts.forEach((p, i) => { const X = x(p[0]), Y = y(vs[i]); if (i) c.lineTo(X, Y); else c.moveTo(X, Y); }); c.stroke();
  c.fillStyle = css('--muted'); c.font = `10.5px ${css('--mono')}`;
  c.fillText(`+${fmt(hi, 1)} µm`, 6, 11); c.fillText(`${fmt(lo, 1)} µm`, 6, H - 3);
  $('#trend-lbl').textContent = `Last 10 s relative to mean ${fmt(mean, 4)} mm · σ ${fmt(sd, 2)} µm (50 ms bins)`;
}

/* ================================================================ scan tab */
let recipe = { kind: 'sample', regions: [] };
function regionCard(r, i) {
  return `<div class="region" data-i="${i}">
    <div class="row"><input class="grow" data-k="name" value="${esc(r.name)}"><button class="btn sm ghost" data-rdel="${i}" title="Remove region">✕</button></div>
    <div class="coords">
      <span>A</span><input type="number" step="any" data-k="x0" value="${r.x0 ?? ''}" placeholder="X"><input type="number" step="any" data-k="y0" value="${r.y0 ?? ''}" placeholder="Y"><button class="btn sm" data-here="0">A = here</button>
      <span>B</span><input type="number" step="any" data-k="x1" value="${r.x1 ?? ''}" placeholder="X"><input type="number" step="any" data-k="y1" value="${r.y1 ?? ''}" placeholder="Y"><button class="btn sm" data-here="1">B = here</button>
    </div>
    <div class="muted small mono">${r.x0 != null && r.x1 != null ? `${fmt(Math.abs(r.x1 - r.x0), 2)} × ${fmt(Math.abs((r.y1 ?? 0) - (r.y0 ?? 0)), 2)} mm` : 'set both corners'}</div>
  </div>`;
}
function renderRegions() { $('#regions').innerHTML = recipe.regions.map(regionCard).join('') || '<div class="muted small">No regions. Add one, then set its corners.</div>'; }
$('#r-addreg').addEventListener('click', () => {
  const w = S.m ? S.m.wpos : [0, 0, 0];
  recipe.regions.push({ name: `Region ${recipe.regions.length + 1}`, x0: +w[0].toFixed(3), y0: +w[1].toFixed(3), x1: null, y1: null });
  renderRegions(); scheduleEstimate();
});
$('#regions').addEventListener('input', (e) => {
  const card = e.target.closest('.region'); if (!card) return;
  const r = recipe.regions[+card.dataset.i], k = e.target.dataset.k;
  if (!k) return;
  r[k] = k === 'name' ? e.target.value : num(e.target.value);
  scheduleEstimate();
});
$('#regions').addEventListener('click', (e) => {
  const d = e.target.closest('[data-rdel]'), h = e.target.closest('[data-here]');
  if (d) { recipe.regions.splice(+d.dataset.rdel, 1); renderRegions(); scheduleEstimate(); }
  if (h) {
    if (!S.m || !S.m.connected) { toast('warn', 'Connect first.'); return; }
    const r = recipe.regions[+h.closest('.region').dataset.i];
    const w = S.m.wpos;
    if (h.dataset.here === '0') { r.x0 = +w[0].toFixed(3); r.y0 = +w[1].toFixed(3); } else { r.x1 = +w[0].toFixed(3); r.y1 = +w[1].toFixed(3); }
    renderRegions(); scheduleEstimate();
  }
});
$('#r-kind').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; recipe.kind = b.dataset.v; $$('#r-kind button').forEach((x) => x.classList.toggle('on', x === b)); });
function readRecipe() {
  return {
    name: $('#r-name').value || 'scan', sample_id: $('#r-sample').value, kind: recipe.kind,
    regions: recipe.regions.map((r) => ({ ...r })),
    pitch_x: num($('#r-px').value), pitch_y: num($('#r-py').value), pattern: $('#r-pattern').value,
    samples: num($('#r-samples').value), settle_ms: num($('#r-settle').value), feed: num($('#r-feed').value),
    drift_every: num($('#r-drift').value), retries: num($('#r-retries').value), max_std_um: num($('#r-maxstd').value),
    max_invalid_frac: recipe.max_invalid_frac, approach_mm: recipe.approach_mm,
    baseline: $('#r-baseline').value || null, notes: $('#r-notes').value, rotation_deg: num($('#r-rot').value) || 0,
  };
}
function writeRecipe(r) {
  $('#r-name').value = r.name || ''; $('#r-sample').value = r.sample_id || ''; recipe.kind = r.kind || 'sample';
  $$('#r-kind button').forEach((x) => x.classList.toggle('on', x.dataset.v === recipe.kind));
  recipe.regions = (r.regions || []).map((g) => ({ ...g }));
  const set = (id, v) => { if (v !== undefined && v !== null) $(id).value = v; };
  set('#r-px', r.pitch_x); set('#r-py', r.pitch_y); set('#r-pattern', r.pattern); set('#r-samples', r.samples); set('#r-settle', r.settle_ms);
  set('#r-feed', r.feed); set('#r-drift', r.drift_every); set('#r-retries', r.retries); set('#r-maxstd', r.max_std_um); set('#r-notes', r.notes);
  set('#r-rot', r.rotation_deg ?? 0);
  recipe.max_invalid_frac = r.max_invalid_frac; recipe.approach_mm = r.approach_mm;
  if (r.baseline) $('#r-baseline').value = r.baseline;
  renderRegions(); scheduleEstimate();
}
let estT = null;
function scheduleEstimate() { clearTimeout(estT); estT = setTimeout(doEstimate, 350); }
['#r-px', '#r-py', '#r-pattern', '#r-samples', '#r-settle', '#r-feed', '#r-drift', '#r-rot'].forEach((s) => $(s).addEventListener('input', scheduleEstimate));
async function doEstimate(show) {
  const r = readRecipe();
  const ready = r.regions.length && r.regions.every((g) => ['x0', 'y0', 'x1', 'y1'].every((k) => g[k] != null));
  if (!ready) { $('#r-est').textContent = r.regions.length ? 'Set both corners of every region.' : 'Add a region.'; $('#r-errs').innerHTML = ''; S.overlay.plan = null; Map2.request(); return null; }
  try {
    const res = await fetch('/api/scan/estimate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(r) });
    const j = await res.json();
    if (!res.ok || j.ok === false) { $('#r-est').textContent = j.error || 'Invalid recipe'; return null; }
    const e = j.estimate;
    $('#r-est').innerHTML = `${e.points.toLocaleString()} points · about ${dur(e.seconds)} · ${fmt(e.per_point_s, 2)} s/pt · scan Z ${fmt(j.z_mach, 3)} (machine)`;
    $('#r-errs').innerHTML = j.errors.map((x) => `<div class="e">✕ ${esc(x)}</div>`).join('');
    S.overlay.plan = { pts: j.points, regions: r.regions, outlines: j.outlines || [], errors: j.errors, pitch: Math.min(r.pitch_x || 1, r.pitch_y || r.pitch_x || 1) };
    if (show) Map2.fitJob();
    Map2.request();
    return j;
  } catch (_) { return null; }
}
$('#r-preview').addEventListener('click', () => { setCenter('map'); doEstimate(true); });
$('#r-start').addEventListener('click', async () => {
  const r = readRecipe();
  const j = await doEstimate(false);
  if (!j) { toast('warn', 'Fix the recipe first.'); return; }
  if (j.errors.length) { toast('error', 'Scan goes outside the allowed travel.'); return; }
  try {
    await post('/api/scan/start', r);
    S.overlay.live = { session: null, pts: [], pitch: S.overlay.plan.pitch, title: 'Height vs first point', unit: 'µm' };
    setCenter('map');
  } catch (_) { }
});
async function loadRecipes() {
  try { const j = await api('/api/recipes'); $('#r-lib').innerHTML = '<option value="">Saved recipes…</option>' + j.recipes.map((n) => `<option>${esc(n)}</option>`).join(''); } catch (_) { }
}
$('#r-save').addEventListener('click', async () => { try { const j = await post('/api/recipes', readRecipe()); toast('ok', `Recipe saved: ${j.name}`); loadRecipes(); } catch (_) { } });
$('#r-load').addEventListener('click', async () => { const n = $('#r-lib').value; if (!n) return; try { writeRecipe(await api(`/api/recipes/${encodeURIComponent(n)}`)); } catch (_) { } });
$('#r-del').addEventListener('click', async () => { const n = $('#r-lib').value; if (!n) return; if (!confirm(`Delete recipe ${n}?`)) return; await api(`/api/recipes/${encodeURIComponent(n)}`, undefined, 'DELETE').catch(() => { }); loadRecipes(); });
$('#r-export').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(readRecipe(), null, 2)], { type: 'application/json' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `${(readRecipe().name || 'recipe').replace(/[^\w.-]+/g, '_')}.json`; a.click();
});
$('#r-import').addEventListener('click', () => $('#r-file').click());
$('#r-file').addEventListener('change', async () => {
  const f = $('#r-file').files[0]; if (!f) return;
  try { writeRecipe(JSON.parse(await f.text())); toast('ok', `Loaded ${f.name}`); } catch (e) { toast('error', `Not a recipe: ${e.message}`); }
  $('#r-file').value = '';
});

/* ---- find the silicon */
let LOC = { templates: {}, align: null };
async function loadAlign(jobEvt) {
  try {
    const j = await api('/api/locate/templates');
    LOC.templates = j.templates; LOC.align = j.align;
  } catch (_) { return; }
  const a = LOC.align, b = $('#loc-badge');
  const fit = jobEvt && jobEvt.result && jobEvt.result.fit;
  if (a) { b.className = 'badge ok'; b.textContent = `ALIGNED ${String(a.time || '').slice(11, 16)}`; }
  const rows = [];
  if (a) rows.push(['Centre (machine)', `${fmt(a.center_machine[0], 3)}, ${fmt(a.center_machine[1], 3)}`], ['Size found', `${fmt(a.width, 3)} × ${fmt(a.height, 3)} mm`], ['Rotation', `${fmt(a.angle_deg, 3)}°`]);
  if (fit) {
    if (fit.source) rows.unshift(['From', fit.source]);
    if (fit.size_error_mm) rows.push(['vs expected', `${fmt(fit.size_error_mm[0], 3)} / ${fmt(fit.size_error_mm[1], 3)} mm`]);
    if (fit.worst_corner_mm != null) rows.push(['Corners vs true rectangle', `worst ${fmt(fit.worst_corner_mm, 3)} mm · diagonals differ ${fmt(fit.diagonal_mismatch_mm, 3)} mm`]);
    if (fit.change_from_previous_mm) rows.push(['Refine moved centre by', `${fmt(fit.change_from_previous_mm.centre[0], 3)}, ${fmt(fit.change_from_previous_mm.centre[1], 3)} mm`]);
    rows.push(['Edge angle spread', `${fmt(fit.angle_spread_deg, 3)}°`]);
    const ch = (fit.profiles || []).map((p) => p.change_mm).filter((v) => v != null);
    if (ch.length) rows.push(['Change at edges', `${fmt(Math.min(...ch), 2)} … ${fmt(Math.max(...ch), 2)} mm`]);
    if (fit.warnings && fit.warnings.length) rows.push(['Check', fit.warnings.join('; ')]);
    if (!fit.ok || (fit.warnings && fit.warnings.length)) { b.className = fit.ok ? 'badge warn' : 'badge err'; b.textContent = fit.ok ? 'ALIGNED, CHECK' : 'CHECK RESULT'; }
  }
  $('#loc-res').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
}
$('#loc-tpl').addEventListener('change', () => {
  const t = LOC.templates[$('#loc-tpl').value];
  if (t) { $('#loc-w').value = t.width; $('#loc-h').value = t.height; }
});
$('#loc-swap').addEventListener('click', () => { const w = $('#loc-w').value; $('#loc-w').value = $('#loc-h').value; $('#loc-h').value = w; });
const locParams = () => ({ width: num($('#loc-w').value), height: num($('#loc-h').value), threshold: num($('#loc-step').value),
  fine_threshold: num($('#loc-fine').value), reach: num($('#loc-search').value) });
$('#loc-go').addEventListener('click', () => post('/api/locate/silicon', locParams()).then(() => setCenter('map')).catch(() => { }));
$('#loc-refine').addEventListener('click', () => post('/api/locate/refine', locParams()).then(() => setCenter('map')).catch(() => { }));

/* corners by eye: stored in MACHINE X/Y (survive a change of work zero), shown as work X/Y */
const CORNER_KEYS = ['bl', 'br', 'fl', 'fr'];
const CORNER_LABEL = { bl: 'Back-left', br: 'Back-right', fl: 'Front-left', fr: 'Front-right' };
const CORNER_HINT = { bl: '−X +Y', br: '+X +Y', fl: '−X −Y', fr: '+X −Y' };
S.corners = (() => { try { return JSON.parse(localStorage.getItem('bm.corners') || 'null'); } catch (_) { return null; } })() || {};
function saveCorners() { try { localStorage.setItem('bm.corners', JSON.stringify(S.corners)); } catch (_) { } Map2.request(); }
function renderCorners() {
  const w = (S.m && S.m.wco) || [0, 0, 0];
  $('#corners').innerHTML = CORNER_KEYS.map((k) => {
    const p = S.corners[k];
    const wx = p ? (p[0] - w[0]).toFixed(3) : '', wy = p ? (p[1] - w[1]).toFixed(3) : '';
    return `<div class="row" style="flex-wrap:nowrap"><span style="width:92px" class="small"><b>${CORNER_LABEL[k]}</b><br><span class="muted">${CORNER_HINT[k]}</span></span>
      <input type="number" step="any" data-cx="${k}" value="${wx}" placeholder="work X" style="width:88px">
      <input type="number" step="any" data-cy="${k}" value="${wy}" placeholder="work Y" style="width:88px">
      <button class="btn sm" data-chere="${k}">= here</button></div>`;
  }).join('');
}
$('#corners').addEventListener('click', (e) => {
  const b = e.target.closest('[data-chere]'); if (!b) return;
  if (!S.m || !S.m.connected) { toast('warn', 'Connect first.'); return; }
  S.corners[b.dataset.chere] = [+S.m.mpos[0].toFixed(4), +S.m.mpos[1].toFixed(4)];
  saveCorners(); renderCorners();
});
$('#corners').addEventListener('change', (e) => {
  const k = e.target.dataset.cx || e.target.dataset.cy; if (!k) return;
  const w = (S.m && S.m.wco) || [0, 0, 0];
  const x = num($(`[data-cx="${k}"]`).value), y = num($(`[data-cy="${k}"]`).value);
  S.corners[k] = x == null || y == null ? null : [x + w[0], y + w[1]];
  saveCorners();
});
$('#c-clear').addEventListener('click', () => { S.corners = {}; saveCorners(); renderCorners(); });
$('#c-use').addEventListener('click', async () => {
  const pts = CORNER_KEYS.map((k) => S.corners[k]).filter(Boolean);
  if (pts.length !== 4) { toast('warn', `Set all 4 corners (${pts.length}/4 so far).`); return; }
  try {
    const j = await post('/api/locate/manual', { corners: pts, width: num($('#loc-w').value), height: num($('#loc-h').value) });
    loadAlign({ result: { fit: j.fit } });
    setTimeout(renderCorners, 400);
    setCenter('map');
  } catch (_) { }
});
renderCorners();
$('#tr-go').addEventListener('click', () => post('/api/locate/trace', {
  feed: num($('#tr-feed').value) || 300, offset_mm: num($('#tr-off').value) || 0, loops: num($('#tr-loops').value) || 1, stop_at_corners: $('#tr-stop').checked,
}).then(() => setCenter('map')).catch(() => { }));
$('#loc-clear').addEventListener('click', async () => {
  if (!confirm('Forget the found die outline?')) return;
  try { await post('/api/locate/clear'); LOC.align = null; $('#loc-badge').className = 'badge mut'; $('#loc-badge').textContent = 'NOT ALIGNED'; $('#loc-res').innerHTML = ''; } catch (_) { }
});
$('#loc-fill').addEventListener('click', () => {
  const a = LOC.align, inset = num($('#loc-inset').value) ?? 0.5;
  const W = a ? a.width : num($('#loc-w').value), H = a ? a.height : num($('#loc-h').value);
  if (!W || !H) { toast('warn', 'Find the silicon first (or enter its size).'); return; }
  recipe.regions = [{ name: 'Silicon', x0: +(-W / 2 + inset).toFixed(3), y0: +(-H / 2 + inset).toFixed(3), x1: +(W / 2 - inset).toFixed(3), y1: +(H / 2 - inset).toFixed(3) }];
  $('#r-rot').value = a ? +a.angle_deg.toFixed(4) : 0;
  if (!a) toast('info', 'Not aligned yet: region uses the nominal size around the current work zero, rotation 0.');
  renderRegions(); doEstimate(true);
});

/* ---- Standard data collection: best settings for a full die height map */
const STANDARD = {
  pattern: 'raster',      // same approach direction every row: no row-to-row striping
  approach_mm: 0.5,       // take up backlash before each row
  samples: 200,           // 0.2 s at ~1 kHz; noise 0.21 um -> mean good to ~0.02 um
  settle_ms: 400,         // after GRBL reports Idle: lets the gantry stop ringing (1 mm hop ~0.3 s at 50 mm/s2)
  feed: 1000,             // 1 mm hops never reach full speed
  drift_every: 100,       // ~1.5 min between drift checks on a ~50 min scan
  retries: 2,
  max_std_um: 3,          // >10x normal noise = vibration or an edge -> re-measure
  max_invalid_frac: 0.05, // the die should read 100 % valid
};
function stdChecks() {
  const out = [];
  const m = S.m, l = S.l, a = LOC.align;
  if (!a) out.push(['e', 'No die outline: do Die outline (corners / refine) and verify it with Trace outline first.']);
  if (!m || !m.connected) out.push(['e', 'CNC not connected.']);
  else {
    if (!m.homed) out.push(['e', 'Machine not homed.']);
    if (!m.hold_motors) out.push(['w', '$1 is not 255: motors relax between points. Machine tab → Set $1=255 (recommended).']);
  }
  if (!l || !l.running) out.push(['e', 'Laser not connected.']);
  else {
    const lr = l.latest;
    if (!lr || !lr.valid) out.push(['w', 'Laser has no reading here: put it over the die and set the standoff.']);
    else if (lr.pos_pct < 25 || lr.pos_pct > 75) out.push(['w', `Laser is at ${fmt(lr.distance_mm, 2)} mm (${Math.round(lr.pos_pct)} % of range): set the standoff near mid-range on the die.`]);
  }
  return out;
}
$('#std-fill').addEventListener('click', async () => {
  await loadAlign();
  const a = LOC.align;
  const checks = stdChecks();
  $('#std-checks').innerHTML = checks.map(([k, t]) => `<div class="${k}">${k === 'e' ? '✕' : '!'} ${esc(t)}</div>`).join('') ||
    '<div style="color:var(--ok)">✓ Outline, homing, $1=255 and standoff all OK.</div>';
  if (!a) { toast('warn', 'Make and verify the die outline first.'); return; }
  const inset = num($('#std-inset').value) ?? 0.5, pitch = num($('#std-pitch').value) || 1;
  const hx = a.width / 2 - inset, hy = a.height / 2 - inset;
  if (hx <= pitch || hy <= pitch) { toast('error', 'Inset too large for this die.'); return; }
  const sn = $('#std-sn').value.trim();
  writeRecipe({
    name: `Die map ${sn || ''} ${new Date().toISOString().slice(0, 10)}`.replace(/\s+/g, ' ').trim(), sample_id: sn, kind: 'sample',
    regions: [{ name: 'Die', x0: +(-hx).toFixed(3), y0: +(-hy).toFixed(3), x1: +hx.toFixed(3), y1: +hy.toFixed(3) }],
    pitch_x: pitch, pitch_y: pitch, rotation_deg: +a.angle_deg.toFixed(4),
    notes: `Standard data collection. Outline ${fmt(a.width, 3)} x ${fmt(a.height, 3)} mm (${a.source || ''} ${a.time || ''}), inset ${inset} mm.`,
    ...STANDARD,
  });
  const j = await doEstimate(true);
  if (j) toast('ok', `Standard data collection set: ${j.estimate.points.toLocaleString()} points, about ${dur(j.estimate.seconds)}. Preview, then Start.`);
});

/* ---- Start fresh: clear the grid / recipe / overlays (and optionally the outline) for a new kind of measurement */
function resetRecipe() {
  const sc = (S.cfg && S.cfg.scan) || {};
  writeRecipe({
    name: 'Scan', sample_id: '', kind: 'sample', regions: [], pitch_x: 1, pitch_y: 1, pattern: sc.pattern || 'serpentine',
    samples: sc.samples ?? 100, settle_ms: sc.settle_ms ?? 300, feed: sc.feed ?? 1500, drift_every: sc.drift_every ?? 50,
    retries: sc.retries ?? 2, max_std_um: sc.max_std_um ?? 10, rotation_deg: 0, notes: '',
  });
  recipe.max_invalid_frac = undefined; recipe.approach_mm = undefined;
  $('#r-baseline').value = ''; $('#r-lib').value = ''; $('#r-errs').innerHTML = ''; $('#std-checks').innerHTML = ''; $('#std-sn').value = '';
}
$('#fresh-go').addEventListener('click', async () => {
  const what = { recipe: $('#fresh-recipe').checked, map: $('#fresh-map').checked, outline: $('#fresh-outline').checked, corners: $('#fresh-corners').checked };
  const names = Object.entries({ recipe: 'recipe & grid', map: 'map overlays', outline: 'die outline', corners: 'corners' }).filter(([k]) => what[k]).map(([, v]) => v);
  if (!names.length) { toast('info', 'Tick what to clear first.'); return; }
  if (S.m && S.m.job && ['running', 'paused'].includes(S.m.job.status)) { toast('warn', 'A job is running: stop it first.'); return; }
  if (!confirm(`Clear: ${names.join(', ')}?\n\nSaved scans, work zero and the machine are not touched.`)) return;
  if (what.recipe) resetRecipe();
  if (what.map) { S.overlay.program = null; S.overlay.plan = null; S.overlay.mpoints = null; S.overlay.live = { session: null, pts: [] }; S.target = null; $('#targetchip').classList.add('hidden'); }
  if (what.outline) {
    try { await post('/api/locate/clear'); LOC.align = null; $('#loc-badge').className = 'badge mut'; $('#loc-badge').textContent = 'NOT ALIGNED'; $('#loc-res').innerHTML = ''; } catch (_) { }
  }
  if (what.corners) { S.corners = {}; saveCorners(); renderCorners(); }
  Map2.request();
  toast('ok', `Cleared: ${names.join(', ')}.`);
});

/* ================================================================ programs tab */
async function loadPrograms() {
  try {
    const j = await api('/api/programs');
    $('#p-table tbody').innerHTML = j.programs.map((p) => `<tr class="click${p.name === S.selProgram ? ' sel' : ''}" data-prog="${esc(p.name)}"><td>${esc(p.name)}</td><td><span class="badge ${p.kind === 'gcode' ? 'info' : 'ok'}">${p.kind === 'gcode' ? 'G-code' : 'measure'}</span></td><td class="muted small">${esc(p.modified)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No programs yet. Import one above.</td></tr>';
  } catch (_) { }
}
$('#p-table').addEventListener('click', (e) => { const r = e.target.closest('[data-prog]'); if (r) selectProgram(r.dataset.prog); });
async function selectProgram(name) {
  S.selProgram = name; loadPrograms();
  $('#p-detail').innerHTML = '<div class="muted">Checking…</div>';
  try { const j = await api(`/api/programs/${encodeURIComponent(name)}`); showProgram(j.analysis, j.text); } catch (_) { }
}
function showProgram(a, text) {
  const errs = a.errors || [], warns = a.warnings || [];
  const isG = a.kind === 'gcode';
  const b = a.bounds;
  $('#p-detail').innerHTML = `
    <div class="sec-h"><h3>${esc(a.name)}</h3><span class="badge ${a.ok ? 'ok' : 'err'}">${a.ok ? 'READY' : `${errs.length} PROBLEM${errs.length === 1 ? '' : 'S'}`}</span></div>
    ${a.description ? `<div class="small">${esc(a.description)}</div>` : ''}
    <dl class="kv">
      ${isG ? `<dt>Lines</dt><dd>${a.n_lines}</dd><dt>Cut / rapid length</dt><dd>${fmt(a.length_mm.feed, 0)} / ${fmt(a.length_mm.rapid, 0)} mm</dd>` : `<dt>Steps</dt><dd>${a.steps}</dd><dt>Measurements</dt><dd>${a.measures}</dd>`}
      <dt>Estimated time</dt><dd>${dur(a.est_s)}</dd>
      ${b ? `<dt>X range (machine)</dt><dd>${fmt(b.min[0], 2)} … ${fmt(b.max[0], 2)}</dd><dt>Y range</dt><dd>${fmt(b.min[1], 2)} … ${fmt(b.max[1], 2)}</dd><dt>Z range</dt><dd>${fmt(b.min[2], 2)} … ${fmt(b.max[2], 2)}</dd>` : ''}
    </dl>
    <div class="msgs">${errs.map((e) => `<div class="e">✕ ${e.line ? `line ${e.line}: ` : ''}${esc(e.text)}</div>`).join('')}${warns.map((w) => `<div class="w">! ${w.line ? `line ${w.line}: ` : ''}${esc(w.text)}</div>`).join('')}</div>
    <div class="row">
      <button class="btn" id="pd-show">Show on map</button>
      ${isG ? '<button class="btn" id="pd-check" title="GRBL parses every line with motion disabled ($C)">Dry run (check mode)</button>' : ''}
      <span class="grow"></span>
      <button class="btn ghost danger" id="pd-del">Delete</button>
      <button class="btn primary" id="pd-run" ${a.ok ? '' : 'disabled'}>▶ Run</button>
    </div>
    <details><summary>Source</summary><pre class="src">${esc((text || '').split('\n').slice(0, 400).join('\n'))}</pre></details>`;
  const show = () => {
    S.overlay.program = { segs: a.preview || [], name: a.name };
    S.overlay.mpoints = (a.points || []).map((p) => [p[0], p[1], p[2]]);
    setCenter('map'); Map2.fitJob();
  };
  $('#pd-show').onclick = show;
  if ($('#pd-check')) $('#pd-check').onclick = () => post(`/api/programs/${encodeURIComponent(a.name)}/run`, { mode: 'check' }).catch(() => { });
  $('#pd-run').onclick = async () => { show(); try { await post(`/api/programs/${encodeURIComponent(a.name)}/run`, { mode: 'run' }); } catch (_) { } };
  $('#pd-del').onclick = async () => { if (!confirm(`Delete ${a.name}? (moved to programs/_deleted)`)) return; await api(`/api/programs/${encodeURIComponent(a.name)}`, undefined, 'DELETE').catch(() => { }); S.selProgram = null; $('#p-detail').innerHTML = '<div class="muted">Select a program.</div>'; loadPrograms(); };
  S.overlay.program = { segs: a.preview || [], name: a.name };
  S.overlay.mpoints = (a.points || []).map((p) => [p[0], p[1], p[2]]);
  Map2.request();
}
async function importFile(f) {
  if (!f) return;
  if (f.size > 50e6) { toast('error', 'File larger than 50 MB.'); return; }
  try {
    const j = await post('/api/programs/import', { filename: f.name, text: await f.text() });
    toast(j.analysis.ok ? 'ok' : 'warn', `Imported ${j.name}${j.analysis.ok ? '' : ' (has problems, see details)'}`);
    S.selProgram = j.name; await loadPrograms(); showProgram(j.analysis, await f.text());
  } catch (_) { }
}
$('#p-drop').addEventListener('click', () => $('#p-file').click());
$('#p-file').addEventListener('change', () => { importFile($('#p-file').files[0]); $('#p-file').value = ''; });
['dragenter', 'dragover'].forEach((ev) => $('#p-drop').addEventListener(ev, (e) => { e.preventDefault(); $('#p-drop').classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => $('#p-drop').addEventListener(ev, (e) => { e.preventDefault(); $('#p-drop').classList.remove('over'); }));
$('#p-drop').addEventListener('drop', (e) => importFile(e.dataTransfer.files[0]));
$('#p-folder').addEventListener('click', () => post('/api/open_folder', { which: 'programs' }).catch(() => { }));

/* ================================================================ results */
async function loadSessions() {
  try {
    const j = await api('/api/sessions');
    S.sessions = j.sessions;
    $('#s-table tbody').innerHTML = j.sessions.map((s) => {
      const bow = s.summary && s.summary.bow_um != null ? fmt(s.summary.bow_um, 2) : '';
      const st = s.status === 'done' ? '' : ` <span class="badge ${s.status === 'running' ? 'info' : 'warn'}">${esc(s.status)}</span>`;
      return `<tr class="click${s.id === S.selSession ? ' sel' : ''}" data-sid="${esc(s.id)}"><td class="small mono">${esc((s.created || '').slice(5, 16))}</td><td>${esc(s.name)}${st}</td><td><span class="badge ${s.kind === 'baseline' ? 'warn' : s.kind === 'points' ? 'mut' : 'info'}">${esc(s.kind)}</span></td><td class="n">${s.n_done ?? ''}</td><td class="n">${bow}</td></tr>`;
    }).join('') || '<tr><td colspan="5" class="muted">No sessions yet.</td></tr>';
    const opts = '<option value="">None</option>' + j.sessions.filter((s) => s.kind === 'baseline' && s.status === 'done').map((s) => `<option value="${esc(s.id)}">${esc(s.name)} · ${esc((s.created || '').slice(0, 16))}</option>`).join('');
    const cur = $('#r-baseline').value; $('#r-baseline').innerHTML = opts; $('#r-baseline').value = cur;
  } catch (_) { }
}
$('#s-table').addEventListener('click', (e) => { const r = e.target.closest('[data-sid]'); if (r) selectSession(r.dataset.sid); });
$('#s-refresh').addEventListener('click', loadSessions);
$('#s-folder').addEventListener('click', () => post('/api/open_folder', { which: 'scans' }).catch(() => { }));

async function selectSession(sid) {
  S.selSession = sid; loadSessions();
  $('#s-detail').innerHTML = '<div class="muted">Loading…</div>';
  try { const j = await api(`/api/sessions/${encodeURIComponent(sid)}`); showSession(j); } catch (_) { }
}
function metricRows(o) {
  if (!o || !o.ok) return `<div class="muted small">${esc((o && o.reason) || 'Not enough valid points.')}</div>`;
  const r = [
    ['Bow (signed)', o.bow_um != null ? `${fmt(o.bow_um, 2)} µm` : '--'], ['Shape', o.shape || '--'],
    ['Warpage (PV after plane)', `${fmt(o.warpage_pv_um, 2)} µm`], ['RMS after plane', `${fmt(o.rms_um, 2)} µm`],
    ['Tilt', `${fmt(o.tilt_mrad, 3)} mrad @ ${fmt(o.tilt_dir_deg, 0)}°`], ['Slope X / Y', `${fmt(o.slope_x_um_per_mm, 3)} / ${fmt(o.slope_y_um_per_mm, 3)} µm/mm`],
    ['Radius X / Y (− = dome)', `${o.radius_x_m != null ? fmt(o.radius_x_m, 1) : '--'} / ${o.radius_y_m != null ? fmt(o.radius_y_m, 1) : '--'} m`],
    ['Twist', o.twist_um != null ? `${fmt(o.twist_um, 2)} µm` : '--'], ['Valid points', `${o.n_valid} / ${o.n_points ?? o.n_valid}`],
  ];
  return `<dl class="kv">${r.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
}
function showLocateSession(j) {
  const m = j.meta, f = (j.results && j.results.locate) || null;
  const rows = f ? [['Source', f.source || '--'], ['Centre (machine)', f.center_machine ? `${fmt(f.center_machine[0], 3)}, ${fmt(f.center_machine[1], 3)}` : '--'],
    ['Size', `${fmt(f.width, 3)} × ${fmt(f.height, 3)} mm`], ['Rotation', `${fmt(f.angle_deg, 3)}°`], ['Edge angle spread', `${fmt(f.angle_spread_deg, 3)}°`]] : [];
  if (f && f.change_from_previous_mm) rows.push(['Moved vs previous outline', `centre ${fmt(f.change_from_previous_mm.centre[0], 3)}, ${fmt(f.change_from_previous_mm.centre[1], 3)} · size ${fmt(f.change_from_previous_mm.size[0], 3)} / ${fmt(f.change_from_previous_mm.size[1], 3)} mm`]);
  $('#s-detail').innerHTML = `
    <div class="sec-h"><h3>${esc(m.name)}</h3><span class="badge ${m.status === 'done' ? 'ok' : 'err'}">${esc(m.status)}</span></div>
    <div class="small muted">${esc(m.created)} · edge profiles · ${j.n_points} readings</div>
    ${m.error ? `<div class="small" style="color:var(--err)">${esc(m.error)}</div>` : ''}
    ${rows.length ? `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>` : ''}
    ${f && f.warnings && f.warnings.length ? `<div class="small" style="color:var(--warn)">${f.warnings.map(esc).join('<br>')}</div>` : ''}
    <div class="row"><button class="btn primary" id="sd-plot">Show profiles</button><button class="btn" id="sd-map">Show on bed map</button><span class="grow"></span>
      <a class="btn sm ghost" href="/api/sessions/${encodeURIComponent(m.id)}/file/points.csv">points.csv</a>
      <button class="btn sm ghost danger" id="sd-del">Delete</button></div>`;
  $('#sd-plot').onclick = async () => {
    try { const pj = await api(`/api/sessions/${encodeURIComponent(m.id)}/points`); S.rv.data = { ...j, points: pj.points }; setCenter('results'); } catch (_) { }
  };
  $('#sd-map').onclick = async () => {
    try {
      const pj = await api(`/api/sessions/${encodeURIComponent(m.id)}/points`);
      const pts = pj.points.filter((p) => p.x_mach !== '');
      const ds = pts.map((p) => (p.distance_mm === '' ? null : +p.distance_mm));
      const ref = ds.find((v) => v != null) ?? 0;
      S.overlay.live = { session: m.id, title: 'Laser distance change', unit: 'µm', pitch: 0.3, pts: pts.map((p, i) => ({ xm: +p.x_mach, ym: +p.y_mach, v: ds[i] == null ? null : -(ds[i] - ref) * 1000 })) };
      setCenter('map'); Map2.fitJob();
    } catch (_) { }
  };
  $('#sd-del').onclick = async () => { if (!confirm(`Delete ${m.name}? (moved to scans/_deleted)`)) return; await api(`/api/sessions/${encodeURIComponent(m.id)}`, undefined, 'DELETE').catch(() => { }); S.selSession = null; $('#s-detail').innerHTML = '<div class="muted">Select a session.</div>'; loadSessions(); };
}

function drawLocateProfiles(el, j) {
  const fg = css('--fg'), grid = css('--line'), bg = css('--panel');
  const by = {};
  for (const p of j.points || []) { if (p.distance_mm === '') continue; (by[p.region] = by[p.region] || []).push(p); }
  const traces = Object.entries(by).map(([reg, ps]) => {
    const x0 = +ps[0].x_work, y0 = +ps[0].y_work;
    return { name: reg, mode: reg.endsWith('-fine') ? 'lines+markers' : 'lines+markers', marker: { size: reg.endsWith('-fine') ? 4 : 5 },
      x: ps.map((p) => Math.hypot(+p.x_work - x0, +p.y_work - y0) + (reg.startsWith('rough') || reg === 'centre' ? 0 : 0)),
      y: ps.map((p) => +p.distance_mm), text: ps.map((p) => p.label), hovertemplate: '%{text}<br>%{y:.3f} mm<extra>' + reg + '</extra>' };
  });
  $('#rv-title').textContent = `${j.meta.name} · ${j.meta.created} · laser distance along each walk (up = higher surface)`;
  Plotly.react(el, traces, { paper_bgcolor: bg, plot_bgcolor: bg, font: { color: fg, family: css('--sans'), size: 12 }, margin: { l: 60, r: 20, t: 20, b: 50 },
    xaxis: { title: 'mm along the walk (from its first point)', gridcolor: grid }, yaxis: { title: 'laser distance (mm)', autorange: 'reversed', gridcolor: grid }, legend: { font: { size: 10 } } },
    { responsive: true, displaylogo: false });
}

function showSession(j) {
  if (j.meta.kind === 'locate') { showLocateSession(j); return; }
  const m = j.meta, res = j.results || {}, o = res.overall;
  const regs = (res.regions || []).filter((r) => r.ok);
  const corr = res.corrections || {};
  const baseOpts = '<option value="">No baseline</option>' + S.sessions.filter((s) => s.kind === 'baseline' && s.id !== m.id).map((s) => `<option value="${esc(s.id)}"${(res.baseline_id || '') === s.id ? ' selected' : ''}>${esc(s.name)} · ${esc((s.created || '').slice(0, 16))}</option>`).join('');
  const cmpOpts = S.sessions.filter((s) => s.id !== m.id).map((s) => `<option value="${esc(s.id)}">${esc(s.name)} · ${esc((s.created || '').slice(5, 16))}</option>`).join('');
  $('#s-detail').innerHTML = `
    <div class="sec-h"><h3>${esc(m.name)}</h3><span class="badge ${m.status === 'done' ? 'ok' : 'warn'}">${esc(m.status)}</span></div>
    <div class="small muted">${esc(m.created)} · ${esc(m.kind)}${m.sample_id ? ` · ${esc(m.sample_id)}` : ''} · ${j.n_points} points${m.duration_s ? ` · ${dur(m.duration_s)}` : ''}${m.error ? ` · <span style="color:var(--err)">${esc(m.error)}</span>` : ''}</div>
    ${m.notes ? `<div class="small">${esc(m.notes)}</div>` : ''}
    ${metricRows(o)}
    ${regs.length > 1 ? `<div class="tablewrap"><table><thead><tr><th>Region</th><th class="n">Bow µm</th><th class="n">PV µm</th><th class="n">Tilt mrad</th><th class="n">Step vs first µm</th></tr></thead><tbody>${regs.map((r) => `<tr><td>${esc(r.region)}</td><td class="n">${fmt(r.bow_um, 2)}</td><td class="n">${fmt(r.warpage_pv_um, 2)}</td><td class="n">${fmt(r.tilt_mrad, 3)}</td><td class="n">${fmt(r.step_vs_first_um, 2)}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <div class="small muted">Drift correction: ${corr.drift && corr.drift.applied ? `on (${corr.drift.n} checks, span ${fmt(corr.drift.span_um, 2)} µm)` : 'off'} · Baseline: ${corr.baseline && corr.baseline.applied ? `subtracted (flat PV ${fmt(corr.baseline.baseline_pv_um, 2)} µm)` : 'none'}${res.flags && Object.keys(res.flags).length ? ` · Flags: ${Object.entries(res.flags).map(([k, v]) => `${esc(k)} ${v}`).join(', ')}` : ''}</div>
    <div class="row"><select id="sd-base" class="grow">${baseOpts}</select><label class="chk small"><input type="checkbox" id="sd-drift" ${corr.drift && corr.drift.applied === false && corr.drift.n ? '' : 'checked'}> Drift</label><button class="btn sm" id="sd-re">Re-analyse</button></div>
    <div class="row"><button class="btn primary" id="sd-plot">Show plot</button><button class="btn" id="sd-map">Show on bed map</button><span class="grow"></span>
      <a class="btn sm ghost" href="/api/sessions/${encodeURIComponent(m.id)}/file/points.csv">points.csv</a>
      <a class="btn sm ghost" href="/api/sessions/${encodeURIComponent(m.id)}/file/results.json">results.json</a>
      <button class="btn sm ghost" id="sd-folder">Folder</button></div>
    <div class="row"><select id="sd-cmp" class="grow"><option value="">Compare with…</option>${cmpOpts}</select><button class="btn sm" id="sd-cmpgo">Compare</button></div>
    <div id="sd-cmpres" class="small"></div>
    <div class="row"><span class="grow"></span><button class="btn sm ghost danger" id="sd-del">Delete session</button></div>`;
  $('#sd-plot').onclick = () => { S.rv.data = j; setCenter('results'); };
  $('#sd-map').onclick = async () => {
    try {
      const pj = await api(`/api/sessions/${encodeURIComponent(m.id)}/points`);
      const pts = pj.points.filter((p) => p.x_mach !== '');
      const hs = pts.map((p) => (p.height_mm === '' ? null : +p.height_mm));
      const ref = hs.find((v) => v != null) ?? 0;
      S.overlay.live = { session: m.id, title: `${m.name}: height`, unit: 'µm', pitch: (j.recipe && j.recipe.pitch_x) || 1, pts: pts.map((p, i) => ({ xm: +p.x_mach, ym: +p.y_mach, v: hs[i] == null ? null : (hs[i] - ref) * 1000 })) };
      setCenter('map'); Map2.fitJob();
    } catch (_) { }
  };
  $('#sd-re').onclick = async () => { try { await post(`/api/sessions/${encodeURIComponent(m.id)}/analyze`, { baseline: $('#sd-base').value, drift: $('#sd-drift').checked }); selectSession(m.id); loadSessions(); toast('ok', 'Re-analysed.'); } catch (_) { } };
  $('#sd-folder').onclick = () => post('/api/open_folder', { which: m.id }).catch(() => { });
  $('#sd-del').onclick = async () => { if (!confirm(`Delete session ${m.name}? (moved to scans/_deleted)`)) return; await api(`/api/sessions/${encodeURIComponent(m.id)}`, undefined, 'DELETE').catch(() => { }); S.selSession = null; $('#s-detail').innerHTML = '<div class="muted">Select a session.</div>'; loadSessions(); };
  $('#sd-cmpgo').onclick = async () => {
    const b = $('#sd-cmp').value; if (!b) return;
    try {
      const c = await api(`/api/compare?a=${encodeURIComponent(m.id)}&b=${encodeURIComponent(b)}`);
      $('#sd-cmpres').innerHTML = c.ok ? `B − A over ${c.n} matching points (planes removed): mean ${fmt(c.mean_um, 2)} µm · PV ${fmt(c.pv_um, 2)} µm · RMS ${fmt(c.rms_um, 2)} µm` : esc(c.reason);
      if (c.ok) { S.rv.data = { ...j, compare: c }; setCenter('results'); }
    } catch (_) { }
  };
  S.rv.data = j;
  if (S.centerView === 'results') drawResults();
}

function setCenter(v) {
  S.centerView = v;
  $$('#centerview button').forEach((b) => b.classList.toggle('on', b.dataset.v === v));
  $('#resultsview').classList.toggle('hidden', v !== 'results');
  if (v === 'results') drawResults(); else { Map2.resize(); }
}
$('#centerview').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setCenter(b.dataset.v); });
$('#rv-quantity').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; S.rv.q = b.dataset.v; $$('#rv-quantity button').forEach((x) => x.classList.toggle('on', x === b)); drawResults(); });
$('#rv-kind').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; S.rv.kind = b.dataset.v; $$('#rv-kind button').forEach((x) => x.classList.toggle('on', x === b)); drawResults(); });

function drawResults() {
  const el = $('#plot');
  const j = S.rv.data;
  if (typeof Plotly === 'undefined') { el.innerHTML = '<div class="muted" style="padding:20px">Plot library missing (static/vendor/plotly.min.js).</div>'; return; }
  if (j && j.meta && j.meta.kind === 'locate') { drawLocateProfiles(el, j); return; }
  if (!j || !j.results) { $('#rv-title').textContent = 'No session selected: pick one on the Results tab.'; Plotly.purge(el); return; }
  const res = j.results, fg = css('--fg'), grid = css('--line'), bg = css('--panel');
  $('#rv-title').textContent = `${j.meta.name} · ${j.meta.created}${j.compare ? ' · difference vs comparison' : ''}`;
  const layout = {
    paper_bgcolor: bg, plot_bgcolor: bg, font: { color: fg, family: css('--sans'), size: 12 }, margin: { l: 60, r: 20, t: 20, b: 50 },
    xaxis: { title: 'X work (mm)', gridcolor: grid, zerolinecolor: grid }, yaxis: { title: 'Y work (mm)', gridcolor: grid, zerolinecolor: grid, scaleanchor: 'x' },
  };
  const cfg = { responsive: true, displaylogo: false };
  let traces = [];
  if (S.rv.kind === 'drift') {
    const d = (j.drift || []).filter((r) => r.height_mm !== '');
    const h0 = d.length ? +d[0].height_mm : 0;
    traces = [{ x: d.map((r) => +r.t / 60), y: d.map((r) => (+r.height_mm - h0) * 1000), mode: 'lines+markers', line: { color: css('--accent') }, name: 'drift point' }];
    Plotly.react(el, traces, { ...layout, xaxis: { title: 'minutes from start', gridcolor: grid }, yaxis: { title: 'height change (µm)', gridcolor: grid } }, cfg);
    return;
  }
  const cs = 'Viridis';
  if (j.compare) {
    const p = j.compare.points;
    traces = [{ type: 'scatter', mode: 'markers', x: p.map((q) => q[0]), y: p.map((q) => q[1]), marker: { size: 9, symbol: 'square', color: p.map((q) => q[2]), colorscale: 'RdBu', reversescale: true, colorbar: { title: 'B−A µm' } } }];
    Plotly.react(el, traces, layout, cfg);
    return;
  }
  const maps = res.maps || [];
  let lo = Infinity;
  if (S.rv.q === 'height') maps.forEach((g) => g.height_mm.forEach((r) => r.forEach((v) => { if (v != null && v < lo) lo = v; })));
  const zOf = (g) => S.rv.q === 'height' ? g.height_mm.map((r) => r.map((v) => (v == null ? null : (v - lo) * 1000))) : g.residual_um;
  if (maps.length) {
    if (S.rv.kind === '3d') {
      traces = maps.map((g, i) => ({ type: 'surface', x: g.x, y: g.y, z: zOf(g), colorscale: cs, showscale: i === 0, name: g.region, colorbar: { title: 'µm' } }));
      Plotly.react(el, traces, { paper_bgcolor: bg, font: { color: fg }, margin: { l: 0, r: 0, t: 0, b: 0 }, scene: { xaxis: { title: 'X mm', color: fg }, yaxis: { title: 'Y mm', color: fg }, zaxis: { title: 'µm', color: fg }, aspectmode: 'manual', aspectratio: { x: 1, y: 1, z: 0.4 } } }, cfg);
    } else {
      traces = maps.map((g, i) => ({ type: 'heatmap', x: g.x, y: g.y, z: zOf(g), colorscale: cs, showscale: i === 0, name: g.region, colorbar: { title: 'µm' }, hovertemplate: 'X %{x:.3f}<br>Y %{y:.3f}<br>%{z:.2f} µm<extra>' + g.region + '</extra>' }));
      Plotly.react(el, traces, layout, cfg);
    }
  } else {
    const p = (res.points_corrected || []).filter((q) => q.h !== null && q.h !== '' && q.h !== undefined);
    const h0 = p.length ? Math.min(...p.map((q) => +q.h)) : 0;
    traces = [{ type: 'scatter', mode: 'markers+text', x: p.map((q) => +q.x), y: p.map((q) => +q.y), text: p.map((q) => fmt((+q.h - h0) * 1000, 1)), textposition: 'top center', marker: { size: 12, color: p.map((q) => (+q.h - h0) * 1000), colorscale: cs, colorbar: { title: 'µm' } } }];
    Plotly.react(el, traces, layout, cfg);
  }
}
new ResizeObserver(() => { if (S.centerView === 'results' && typeof Plotly !== 'undefined' && $('#plot').data) Plotly.Plots.resize($('#plot')); }).observe($('#mapwrap'));

/* ================================================================ machine tab */
const CFG_FIELDS = [
  ['Connection', [['machine.baud', 'Serial baud', 'num'], ['machine.tcp_host', 'WiFi host', 'text'], ['machine.tcp_port', 'WiFi port', 'num'], ['machine.status_hz', 'Status reports per second', 'num'], ['machine.reset_on_connect', 'Reboot controller when connecting', 'bool']]],
  ['Travel & safety', [['machine.limit_margin_mm', 'Keep this far from the travel ends (mm)', 'num'], ['machine.safe_z_mpos', 'Safe Z, machine (blank = none)', 'num?'], ['machine.z_floor_mpos', 'Z floor, machine (blank = none)', 'num?'], ['machine.require_homing', 'Require homing for go-to, programs, scans', 'bool'], ['machine.block_spindle', 'Block spindle on (M3/M4)', 'bool'], ['machine.unhomed_jog_max_mm', 'Continuous jog length when not homed (mm)', 'num'], ['machine.travel_feed_xy', 'Go-to feed XY (mm/min)', 'num'], ['machine.travel_feed_z', 'Go-to feed Z (mm/min)', 'num']]],
  ['Jog', [['jog.feed_xy', 'Jog feed XY (mm/min)', 'num'], ['jog.feed_z', 'Jog feed Z (mm/min)', 'num'], ['jog.fast_factor', 'Fast ×', 'num'], ['jog.fine_factor', 'Fine ×', 'num'], ['jog.steps', 'Step sizes (comma separated)', 'list'], ['jog.watchdog_s', 'Stop a held jog after this long without the browser (s)', 'num']]],
  ['Display', [['ui.flip_x', 'Mirror X on screen (map + arrows)', 'bool'], ['ui.flip_y', 'Mirror Y on screen (map + arrows)', 'bool']]],
  ['Laser', [['laser.dll_path', 'MEDAQLib.dll (64-bit) path', 'text'], ['laser.interface', 'MEDAQLib interface', 'text'], ['laser.port', 'COM port', 'text'], ['laser.baud', 'Baud (blank = default)', 'num?'], ['laser.smr_mm', 'Start of measuring range (mm)', 'num'], ['laser.range_mm', 'Measuring range (mm)', 'num'], ['laser.reading_zero', 'Reading scale', 'sel:auto=Auto detect,smr=0..range (from range start),face=From sensor face'], ['laser.guard_mm', 'Stop a Z-down move this close to the near end (mm)', 'num']]],
  ['Scan defaults', [['scan.samples', 'Samples per point', 'num'], ['scan.settle_ms', 'Settle time (ms)', 'num'], ['scan.feed', 'Feed (mm/min)', 'num'], ['scan.drift_every', 'Drift check every N points', 'num'], ['scan.retries', 'Retries', 'num'], ['scan.max_invalid_frac', 'Max invalid fraction', 'num'], ['scan.max_std_um', 'Max σ (µm)', 'num'], ['scan.raster_approach_mm', 'Raster approach (mm)', 'num']]],
];
const getPath = (o, p) => p.split('.').reduce((a, k) => (a == null ? a : a[k]), o);
function renderConfigForm() {
  if (!S.cfg) return;
  const lim = S.cfg.machine.limits;
  let h = '<div class="sec-h"><h3>App settings</h3><button class="btn primary sm" id="cfg-save">Save settings</button></div>';
  for (const [title, fields] of CFG_FIELDS) {
    h += `<details ${title === 'Travel & safety' ? 'open' : ''}><summary><b>${title}</b></summary><div style="display:grid;gap:6px;margin:8px 0 4px">`;
    for (const [path, label, type] of fields) {
      const v = getPath(S.cfg, path);
      if (type === 'bool') h += `<label class="chk"><input type="checkbox" data-cfg="${path}" data-type="bool" ${v ? 'checked' : ''}> ${label}</label>`;
      else if (type.startsWith('sel:')) h += `<label class="field"><span>${label}</span><select data-cfg="${path}" data-type="text">${type.slice(4).split(',').map((o) => { const [k, t] = o.split('='); return `<option value="${k}" ${v === k ? 'selected' : ''}>${t}</option>`; }).join('')}</select></label>`;
      else h += `<label class="field"><span>${label}</span><input data-cfg="${path}" data-type="${type}" ${type.startsWith('num') ? 'type="number" step="any"' : ''} value="${esc(type === 'list' ? (v || []).join(', ') : (v ?? ''))}"></label>`;
    }
    h += '</div></details>';
  }
  h += `<details><summary><b>Travel limits (machine)</b> <span class="muted small">from ${esc(S.cfg.machine.limits_source)}</span></summary><div style="display:grid;gap:6px;margin:8px 0">
    <div class="hint">Worked out automatically after every homing from $23, $27 and $130–$132. Edit only if the machine differs; saving here makes them manual.</div>
    ${['x', 'y', 'z'].map((a) => `<div class="grid3"><span class="mono" style="align-self:center">${a.toUpperCase()}</span><input type="number" step="any" data-lim="${a}0" value="${lim[a][0]}"><input type="number" step="any" data-lim="${a}1" value="${lim[a][1]}"></div>`).join('')}
    <div class="row"><button class="btn sm" id="lim-save">Save as manual</button><button class="btn sm ghost" id="lim-auto">Back to automatic</button></div></div></details>`;
  $('#cfg-form').innerHTML = h;
  $('#cfg-save').onclick = saveConfig;
  $('#lim-save').onclick = () => { const L = {}; ['x', 'y', 'z'].forEach((a) => { L[a] = [num($(`[data-lim="${a}0"]`).value), num($(`[data-lim="${a}1"]`).value)]; }); post('/api/config', { machine: { limits: L } }).then(() => toast('ok', 'Manual limits saved.')).catch(() => { }); };
  $('#lim-auto').onclick = () => post('/api/config', { machine: { limits_source: 'settings' } }).then(() => toast('info', 'Limits return to automatic at the next homing.')).catch(() => { });
}
async function saveConfig() {
  const patch = {};
  for (const el of $$('[data-cfg]')) {
    const path = el.dataset.cfg, t = el.dataset.type;
    let v;
    if (t === 'bool') v = el.checked;
    else if (t === 'num') v = num(el.value);
    else if (t === 'num?') v = el.value === '' ? null : num(el.value);
    else if (t === 'list') v = el.value.split(',').map((x) => num(x.trim())).filter((x) => x != null && x > 0);
    else v = el.value;
    if (t === 'num' && v == null) continue;
    const [sec, key] = path.split('.');
    (patch[sec] = patch[sec] || {})[key] = v;
  }
  try { await post('/api/config', patch); toast('ok', 'Settings saved.'); } catch (_) { }
}
let cfgDirty = false;
$('#cfg-form').addEventListener('input', () => { cfgDirty = true; });
function applyConfig(first) {
  if (!S.cfg) return;
  if (first || !cfgDirty) renderConfigForm();
  renderSteps(); relabelPad(); renderPositions();
  if (first) {
    $('#c-transport').value = S.cfg.machine.transport || 'serial';
    $('#c-host').value = S.cfg.machine.tcp_host || '';
    $('#l-port').value = S.cfg.laser.port || '';
    lDriver = S.cfg.laser.driver === 'sim' ? 'sim' : 'medaq';
    $$('#l-driver button').forEach((x) => x.classList.toggle('on', x.dataset.v === lDriver));
    $('#l-port').classList.toggle('hidden', lDriver !== 'medaq');
    const sc = S.cfg.scan;
    $('#r-samples').value = sc.samples; $('#r-settle').value = sc.settle_ms; $('#r-feed').value = sc.feed; $('#r-drift').value = sc.drift_every;
    $('#r-retries').value = sc.retries; $('#r-maxstd').value = sc.max_std_um; $('#r-pattern').value = sc.pattern;
    Jog.step = S.cfg.jog.default_step || 1; markStep();
    connUI(); loadPorts();
  }
  Map2.request();
}
$('#cfg-form').addEventListener('click', (e) => { if (e.target.id === 'cfg-save') cfgDirty = false; });

async function loadGrblSettings() {
  try {
    const j = await api('/api/settings');
    $('#gs-backups').innerHTML = j.backups.map((b) => `<option>${esc(b)}</option>`).join('') || '<option value="">No backups</option>';
    $('#gs-fw').textContent = j.firmware ? `Firmware ${j.firmware.version} · options ${j.firmware.options}` : 'Connect to read the controller settings.';
    $('#gs-table tbody').innerHTML = j.rows.map((r) => `<tr${r.changed ? ' style="background:var(--warn-soft)"' : ''}><td class="mono">$${r.key}</td><td>${esc(r.name)}<div class="muted small">${esc(r.unit)}${r.note ? ' · ' + esc(r.note) : ''}</div></td><td><input data-gs="${r.key}" value="${esc(r.value)}" style="width:86px" class="mono"></td><td class="mono small muted">${r.factory ?? ''}</td><td><button class="btn sm" data-gsset="${r.key}">Set</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">Not connected.</td></tr>';
    const one = j.rows.find((r) => r.key === 1);
    $('#gs-reco').innerHTML = one && +one.value !== 255 ? `<span class="badge warn">TIP</span><span class="small">$1=${esc(one.value)} lets the motors relax after each move. For scanning set $1=255 (motors stay energised).</span><button class="btn sm" data-gsquick="1=255">Set $1=255</button>` : '';
  } catch (_) { }
}
$('#gs-table').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-gsset]'); if (!b) return;
  const k = b.dataset.gsset, v = $(`[data-gs="${k}"]`).value;
  if (!confirm(`Write $${k}=${v} to the controller?`)) return;
  try { await post('/api/settings', { key: +k, value: v }); toast('ok', `$${k}=${v} written.`); loadGrblSettings(); } catch (_) { }
});
$('#gs-reco').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-gsquick]'); if (!b) return;
  const [k, v] = b.dataset.gsquick.split('=');
  try { await post('/api/settings', { key: +k, value: v }); toast('ok', `$${k}=${v} written.`); loadGrblSettings(); } catch (_) { }
});
$('#gs-refresh').addEventListener('click', loadGrblSettings);
$('#gs-backup').addEventListener('click', async () => { try { const j = await post('/api/settings/backup'); toast('ok', `Saved ${j.file}`); loadGrblSettings(); } catch (_) { } });
$('#gs-restore').addEventListener('click', async () => {
  const f = $('#gs-backups').value; if (!f) return;
  if (!confirm(`Write every setting from ${f} that differs from the controller?`)) return;
  try { const j = await post('/api/settings/restore', { file: f }); toast('ok', j.changed.length ? `Restored: ${j.changed.join('; ')}` : 'Nothing differed.'); loadGrblSettings(); } catch (_) { }
});

/* ================================================================ console */
const hist = []; let hi = -1;
async function sendCmd(line) {
  line = (line || '').trim(); if (!line) return;
  hist.push(line); hi = hist.length;
  try { const j = await post('/api/console', { line }); if (j.ok === true && j.message && j.message !== 'ok') toast('warn', j.message); } catch (_) { }
}
$('#cmd').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { sendCmd($('#cmd').value); $('#cmd').value = ''; }
  else if (e.key === 'ArrowUp') { e.preventDefault(); if (hi > 0) { hi--; $('#cmd').value = hist[hi]; } }
  else if (e.key === 'ArrowDown') { e.preventDefault(); if (hi < hist.length - 1) { hi++; $('#cmd').value = hist[hi]; } else { hi = hist.length; $('#cmd').value = ''; } }
});
$('#cmd-send').addEventListener('click', () => { sendCmd($('#cmd').value); $('#cmd').value = ''; });
$$('[data-cmd]').forEach((b) => b.addEventListener('click', () => sendCmd(b.dataset.cmd)));
$('#lg-clear').addEventListener('click', () => { $('#log').innerHTML = ''; });
['#lg-tx', '#lg-rx', '#lg-notes'].forEach((s) => $(s).addEventListener('change', () => $$('#log > div').forEach(applyLogFilter)));

/* ================================================================ tabs + init */
$('#tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#tabs button').forEach((x) => x.classList.toggle('on', x === b));
  $$('.tab').forEach((t) => t.classList.toggle('hidden', t.id !== `tab-${b.dataset.tab}`));
  const t = b.dataset.tab;
  if (t === 'programs') loadPrograms();
  if (t === 'results') loadSessions();
  if (t === 'machine') loadGrblSettings();
  if (t === 'scan') { loadRecipes(); scheduleEstimate(); loadAlign(); }
  if (t === 'laser') drawTrend();
});

Map2.init();
renderRegions();
wsConnect();
loadSessions();
