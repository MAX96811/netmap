/* Homelab map — an infinite board over a scanned inventory.
 *
 * Two data files, kept strictly apart:
 *   data/graph.json   written by collect.py, overwritten on every scan
 *   data/overrides.json  everything you do by hand; the scanner never touches it
 *
 * A node's stored position is relative to its parent group, so re-scanning and
 * re-packing a group does not move the things you placed by hand.
 */
'use strict';

const CARD_W = 178, CARD_H = 44, PAD = 14, HEAD = 26, GAP = 10;

const $ = s => document.querySelector(s);
/** Pointer capture is best-effort — it throws if the pointer is already gone,
 *  and losing it must never abort the gesture. */
const capture = (el, id) => { try { el.setPointerCapture(id); } catch (e) { /* ignore */ } };
const world = $('#world'), svg = $('#edges'), vp = $('#viewport');

let G = { nodes: [], edges: [] };
let OV = blankOverrides();
const N = new Map();     // id -> node
const KIDS = new Map();  // id ('' = root) -> [node]
const BOX = new Map();   // id -> {ox,oy,w,h}
const REL = new Map();   // id -> {x,y} relative to parent
const ABS = new Map();   // id -> {x,y} world
const EL = new Map();    // id -> element

let view = { x: 60, y: 60, z: .55 };
let sel = null, linking = false, dirty = false, showHidden = false;
let layersOff = new Set(), query = '';
let LIVE = {}, liveRev = -1, liveSources = {}, liveTimer = 0, liveFails = 0;

function blankOverrides() {
  return { positions: {}, labels: {}, icons: {}, accents: {}, notes: {},
           hidden: [], collapsed: [], customNodes: [], customEdges: [],
           excalidraw: null, parents: {}, view: null, theme: null };
}

/* ------------------------------------------------------------------ load */
async function boot() {
  const [g, o] = await Promise.all([
    fetch('data/graph.json?t=' + Date.now()).then(r => r.json()),
    fetch('api/overrides?t=' + Date.now()).then(r => r.ok ? r.json() : blankOverrides()).catch(blankOverrides),
  ]);
  G = g;
  if (G.site) document.title = G.site + ' — Map';
  OV = Object.assign(blankOverrides(), o || {});
  pruneOrphans();
  if (OV.theme) document.documentElement.dataset.theme = OV.theme;
  if (OV.view) view = OV.view;
  index();
  buildLayerBar();
  relayout();
  if (!OV.view) fit();
  const p = (G.problems || []).length;
  $('#status').innerHTML = `<b>${G.nodes.length}</b> nodes · <b>${G.edges.length}</b> links · scanned ${
    new Date(G.generated).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` +
    (p ? ` · <span style="color:var(--warn)">${p} warning${p > 1 ? 's' : ''}</span>` : '');
  if (p) $('#status').title = G.problems.join('\n');
  pollLive();
  // Stay lazy only when there is nothing to show. If the board already has
  // annotations they must be visible on load, not after pressing Draw.
  if (OV.excalidraw && (OV.excalidraw.elements || []).length) loadExcalidraw().catch(() => { excalLoading = null; });
}

/** A node's parent, honouring a manual re-home. `null` in OV.parents means
 *  "pulled out to the top level", which is different from "not set". */
function eff(id) {
  const o = (OV.parents || {})[id];
  if (o !== undefined) return o;
  const n = N.get(id);
  return n ? n.parent : null;
}
/** Is `a` an ancestor of `b`? Used to refuse dropping a group inside itself. */
function ancestorOf(a, b) {
  let c = eff(b);
  for (let i = 0; c && i < 64; i++) { if (c === a) return true; c = eff(c); }
  return false;
}

function index() {
  N.clear(); KIDS.clear();
  const all = G.nodes.concat(OV.customNodes || []);
  for (const n of all) N.set(n.id, n);
  for (const n of all) {
    let p = eff(n.id);
    if (p === n.id || (p && !N.has(p)) || (p && ancestorOf(n.id, p))) p = '';
    if (!p) p = '';
    if (!KIDS.has(p)) KIDS.set(p, []);
    KIDS.get(p).push(n);
  }
}

const isGroup = id => { const n = N.get(id); return n && (n.type === 'group' || (KIDS.get(id) || []).length); };
const collapsed = id => OV.collapsed.includes(id);
const hidden = id => OV.hidden.includes(id);
const label = n => OV.labels[n.id] ?? n.label;
const icon = n => OV.icons[n.id] ?? n.icon ?? '';

/** A node is drawn when it is not hidden, its layer is on, and every ancestor
 *  is expanded. Descendants of a collapsed group fold into it. */
function drawn(id) {
  let n = N.get(id);
  if (!n) return false;
  if (hidden(id) && !showHidden) return false;
  if (n.layer && layersOff.has(n.layer)) return false;
  let p = n.parent;
  while (p && N.has(p)) {
    if (collapsed(p) || hidden(p) && !showHidden) return false;
    const pn = N.get(p);
    if (pn.layer && layersOff.has(pn.layer)) return false;
    p = pn.parent;
  }
  return true;
}
function kidsOf(id) { return (KIDS.get(id) || []).filter(k => !(hidden(k.id) && !showHidden) && !(k.layer && layersOff.has(k.layer))); }

/* ---------------------------------------------------------------- layout */
/** Width to pack into so a group comes out roughly `aspect`:1 rather than a
 *  tall ribbon. Driven by the children's total area, not their count, so a
 *  group of big sub-boxes and a group of small cards both come out balanced. */
function packWidth(items, aspect) {
  const area = items.reduce((s, i) => s + (i.w + GAP) * (i.h + GAP), 0);
  const widest = items.reduce((m, i) => Math.max(m, i.w), 0);
  return Math.max(widest, Math.sqrt(area * aspect));
}

function shelf(items, maxW) {
  let x = 0, y = 0, rowH = 0; const out = {};
  for (const it of items) {
    if (x > 0 && x + it.w > maxW) { x = 0; y += rowH + GAP; rowH = 0; }
    out[it.id] = { x, y }; x += it.w + GAP; rowH = Math.max(rowH, it.h);
  }
  return out;
}

function computeBox(id) {
  const kids = collapsed(id) ? [] : kidsOf(id);
  if (!kids.length) {
    const b = (isGroup(id) && !collapsed(id))
      ? { ox: -PAD, oy: -PAD - HEAD, w: 250, h: 110 }     // empty group: a real drop area
      : { ox: 0, oy: 0, w: CARD_W, h: CARD_H };
    BOX.set(id, b); return b;
  }
  const items = kids.map(k => { const b = computeBox(k.id); return { id: k.id, w: b.w, h: b.h, ox: b.ox, oy: b.oy, g: isGroup(k.id) }; });
  items.sort((a, b) => (b.g ? 1 : 0) - (a.g ? 1 : 0));           // boxes first, then cards
  const auto = shelf(items, packWidth(items, 1.9));
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const it of items) {
    const saved = OV.positions[it.id];
    const r = saved ? { x: saved.x, y: saved.y } : { x: auto[it.id].x - it.ox, y: auto[it.id].y - it.oy };
    REL.set(it.id, r);
    x0 = Math.min(x0, r.x + it.ox); y0 = Math.min(y0, r.y + it.oy);
    x1 = Math.max(x1, r.x + it.ox + it.w); y1 = Math.max(y1, r.y + it.oy + it.h);
  }
  const b = { ox: x0 - PAD, oy: y0 - PAD - HEAD, w: (x1 - x0) + PAD * 2, h: (y1 - y0) + PAD * 2 + HEAD };
  BOX.set(id, b); return b;
}

function relayout() {
  BOX.clear(); REL.clear(); ABS.clear();
  const roots = kidsOf('');
  const items = roots.map(r => { const b = computeBox(r.id); return { id: r.id, w: b.w, h: b.h, ox: b.ox, oy: b.oy, g: isGroup(r.id) }; });
  items.sort((a, b) => (b.g ? 1 : 0) - (a.g ? 1 : 0));
  const auto = shelf(items, packWidth(items, 3.0));
  for (const it of items) {
    const saved = OV.positions[it.id];
    REL.set(it.id, saved ? { x: saved.x, y: saved.y } : { x: auto[it.id].x - it.ox, y: auto[it.id].y - it.oy });
  }
  render();
}

function abs(id) {
  if (ABS.has(id)) return ABS.get(id);
  const r = REL.get(id) || { x: 0, y: 0 };
  const pid = eff(id);
  const p = (pid && N.has(pid) && !ancestorOf(id, pid)) ? abs(pid) : { x: 0, y: 0 };
  const a = { x: p.x + r.x, y: p.y + r.y };
  ABS.set(id, a); return a;
}
/** Screen-space rect of a node, in world coordinates. */
function rect(id) {
  const a = abs(id), b = BOX.get(id) || { ox: 0, oy: 0, w: CARD_W, h: CARD_H };
  return { x: a.x + b.ox, y: a.y + b.oy, w: b.w, h: b.h };
}

/* ---------------------------------------------------------------- render */
function render() {
  ABS.clear();
  const keep = new Set();
  const order = [];
  (function walk(pid, depth) {
    for (const n of kidsOf(pid)) {
      order.push([n, depth]);
      if (!collapsed(n.id)) walk(n.id, depth + 1);
    }
  })('', 0);

  for (const [n, depth] of order) {
    if (!drawn(n.id)) continue;
    keep.add(n.id);
    const grp = isGroup(n.id) && !collapsed(n.id);
    let el = EL.get(n.id);
    if (!el || el.dataset.kind !== (grp ? 'g' : 'n')) {
      if (el) el.remove();
      el = document.createElement('div');
      el.dataset.id = n.id; el.dataset.kind = grp ? 'g' : 'n';
      world.appendChild(el); EL.set(n.id, el);
    }
    const r = rect(n.id);
    if (grp) {
      const region = n.id === 'group:pc' ? 'r-pc' : n.id === 'group:nas' ? 'r-nas' : n.id === 'group:ha' ? 'r-ha' : '';
      el.className = `group depth${depth} ${region}`;
      el.style.cssText = `transform:translate(${r.x}px,${r.y}px);width:${r.w}px;height:${r.h}px;z-index:${5 + depth}`;
      el.innerHTML = `<div class="ghead"><span class="chev">▾</span><span class="ic">${esc(icon(n))}</span>` +
        `<span class="glbl">${esc(label(n))}</span><span class="gcount">${kidsOf(n.id).length}</span>` +
        `<span class="live"></span>` +
        (n.sub ? `<span class="gsub">${esc(n.sub)}</span>` : '') + `</div>`;
    } else {
      const st = n.status ? ` s-${n.status}` : '';
      const kids = (KIDS.get(n.id) || []).length;
      el.className = 'node' + st + (hidden(n.id) ? ' ghosted' : '');
      const accent = OV.accents[n.id];
      el.style.cssText = `transform:translate(${r.x}px,${r.y}px);width:${CARD_W}px;min-height:${CARD_H}px` +
        (accent ? `;border-left-color:${accent}` : '');
      el.innerHTML = `<span class="ic">${esc(icon(n))}</span><div class="txt">` +
        `<div class="lbl">${esc(label(n))}</div>` +
        (n.sub ? `<div class="sub">${esc(n.sub)}</div>` : '') + `</div>` +
        `<span class="live"></span>` +
        (n.trap ? `<span class="flag" title="known trap">⚠</span>` : '') +
        (kids && collapsed(n.id) ? `<span class="flag">▸${kids}</span>` : '');
    }
  }
  for (const [id, el] of EL) if (!keep.has(id)) { el.remove(); EL.delete(id); }
  drawEdges(keep);
  applyView();
  applyFilter();
  applyLive();
  drawMini();
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/** Fold an endpoint up to the nearest drawn ancestor, so links survive collapse. */
function anchor(id) {
  let cur = id;
  while (cur && N.has(cur)) { if (EL.has(cur)) return cur; cur = N.get(cur).parent; }
  return null;
}

function drawEdges(keep) {
  const all = G.edges.concat((OV.customEdges || []).map(e => Object.assign({ kind: 'custom' }, e)));
  const seen = new Set(); let out = '';
  for (const e of all) {
    const a = anchor(e.from), b = anchor(e.to);
    if (!a || !b || a === b) continue;
    const key = a + '>' + b + e.kind;
    if (seen.has(key)) continue;
    seen.add(key);
    const ra = rect(a), rb = rect(b);
    const ca = { x: ra.x + ra.w / 2, y: ra.y + ra.h / 2 }, cb = { x: rb.x + rb.w / 2, y: rb.y + rb.h / 2 };
    const pa = edgePoint(ra, cb), pb = edgePoint(rb, ca);
    const dx = Math.abs(pb.x - pa.x) * .45 + 18;
    const horiz = Math.abs(cb.x - ca.x) > Math.abs(cb.y - ca.y);
    const d = horiz
      ? `M${pa.x},${pa.y} C${pa.x + Math.sign(pb.x - pa.x) * dx},${pa.y} ${pb.x - Math.sign(pb.x - pa.x) * dx},${pb.y} ${pb.x},${pb.y}`
      : `M${pa.x},${pa.y} C${pa.x},${pa.y + (pb.y - pa.y) * .45} ${pb.x},${pb.y - (pb.y - pa.y) * .45} ${pb.x},${pb.y}`;
    out += `<path class="k-${e.kind}" data-a="${esc(a)}" data-b="${esc(b)}" d="${d}"></path>`;
  }
  svg.innerHTML = out;
}

/** Where the line from the box centre toward `to` crosses the box border. */
function edgePoint(r, to) {
  const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
  const dx = to.x - cx, dy = to.y - cy;
  if (!dx && !dy) return { x: cx, y: cy };
  const sx = dx ? (r.w / 2 + 4) / Math.abs(dx) : Infinity;
  const sy = dy ? (r.h / 2 + 4) / Math.abs(dy) : Infinity;
  const s = Math.min(sx, sy);
  return { x: cx + dx * s, y: cy + dy * s };
}

function applyView() {
  syncToExcalidraw();
  world.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.z})`;
  vp.style.backgroundSize = `${28 * view.z}px ${28 * view.z}px`;
  vp.style.backgroundPosition = `${view.x}px ${view.y}px`;
}

/* ------------------------------------------------- drawing (Excalidraw) */
/* Real Excalidraw, vendored under web/vendor and loaded only when the Draw
 * button is first pressed — the board itself stays a ~40 KB page.
 *
 * Two infinite canvases have to agree on where "here" is. The board draws at
 *   screen = world * z + view
 * and Excalidraw at
 *   screen = (scene + scroll) * zoom
 * so zoom = z, scroll = view / z keeps a circle drawn around a group sitting
 * on that group at every zoom level. Whichever canvas the pointer is in leads;
 * `syncing` stops the two from chasing each other. */
let excal = null, excalRoot = null, excalOn = false, excalLoading = null, syncing = false;

async function loadExcalidraw() {
  if (excalLoading) return excalLoading;
  excalLoading = (async () => {
    // Excalidraw resolves its fonts against this at runtime; keep it local so
    // the map never reaches out to a CDN.
    window.EXCALIDRAW_ASSET_PATH = new URL('vendor/', location.href).href;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = 'vendor/entry.css';
    document.head.appendChild(css);
    await import('./vendor/entry.js');
    excalRoot = window.ExcalidrawCreateRoot($('#excal'));
    renderExcal();
    requestAnimationFrame(syncToExcalidraw);
  })();
  return excalLoading;
}

function renderExcal() {
  if (!excalRoot) return;
  const { Excalidraw } = window.ExcalidrawLib;
  const saved = OV.excalidraw || {};
  excalRoot.render(window.React.createElement(Excalidraw, {
    excalidrawAPI: api => { excal = api; syncToExcalidraw(); },
    initialData: {
      elements: saved.elements || [],
      files: saved.files || undefined,
      appState: { viewBackgroundColor: 'transparent', currentItemStrokeWidth: 2 },
      scrollToContent: false,
    },
    theme: document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
    viewModeEnabled: !excalOn,
    UIOptions: { canvasActions: {
      loadScene: false, export: false, saveToActiveFile: false,
      changeViewBackgroundColor: false, toggleTheme: false } },
    onChange: onExcalChange,
  }));
}

function onExcalChange(elements, appState, files) {
  if (excalOn) syncFromExcalidraw(appState);
  const live = (elements || []).filter(e => !e.isDeleted);
  const prev = (OV.excalidraw && OV.excalidraw.elements) || [];
  if (live.length === prev.length && JSON.stringify(live) === JSON.stringify(prev)) return;
  OV.excalidraw = { elements: live, files: files || undefined };
  saveSoon();
}

function syncToExcalidraw() {
  if (!excal || syncing) return;
  syncing = true;
  try {
    excal.updateScene({ appState: {
      scrollX: view.x / view.z, scrollY: view.y / view.z, zoom: { value: view.z } } });
  } finally { syncing = false; }
}

function syncFromExcalidraw(st) {
  if (syncing || !st || !st.zoom) return;
  const z = st.zoom.value, nx = st.scrollX * z, ny = st.scrollY * z;
  if (Math.abs(nx - view.x) < .5 && Math.abs(ny - view.y) < .5 && Math.abs(z - view.z) < 1e-4) return;
  syncing = true;
  try {
    view.x = nx; view.y = ny; view.z = z;
    world.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.z})`;
    vp.style.backgroundSize = `${28 * view.z}px ${28 * view.z}px`;
    vp.style.backgroundPosition = `${view.x}px ${view.y}px`;
    drawMini(); saveViewSoon();
  } finally { syncing = false; }
}

$('#b-draw').onclick = async () => {
  const btn = $('#b-draw');
  if (!excalRoot) {
    btn.textContent = '… loading';
    try { await loadExcalidraw(); } catch (e) {
      /* The Excalidraw bundle is not in the repo — it is 22 MB of build
         output. tools/build-vendor.sh produces web/vendor in one command. */
      excalLoading = null;
      btn.textContent = '✏ Draw';
      $('#status').innerHTML = '<span style="color:var(--warn)">drawing needs the Excalidraw bundle — run tools/build-vendor.sh</span>';
      return;
    }
    btn.textContent = '✏ Draw';
  }
  excalOn = !excalOn;
  $('#excal').classList.toggle('on', excalOn);
  btn.classList.toggle('on', excalOn);
  renderExcal();
  requestAnimationFrame(syncToExcalidraw);
};

/* ------------------------------------------------------------------ live */
/* Two independent channels, so they can never be confused:
 *   health   -> the left border, using the reserved status colours
 *   activity -> a filled badge pill and a brighter card, no hue at all
 * "On" is shown by luminance because every warm hue near gold sits too close
 * to the status warning colour to be safe. */
function applyLive() {
  for (const [id, el] of EL) {
    const s = LIVE[id];
    const slot = el.querySelector('.live');
    if (!slot) continue;
    if (!s) { slot.textContent = ''; el.classList.remove('live-on', 'live-known'); continue; }
    slot.textContent = s.b || '';
    slot.title = s.d || '';
    el.classList.toggle('live-on', s.a === 1);
    el.classList.toggle('live-known', true);
    if (s.h) {
      el.classList.remove('s-up', 's-down', 's-warn');
      el.classList.add('s-' + s.h);
    }
  }
}

async function pollLive() {
  clearTimeout(liveTimer);
  const again = ms => { liveTimer = setTimeout(pollLive, ms); };
  if (document.visibilityState !== 'visible') return again(4000);   // idle in a background tab
  try {
    const d = await fetch('api/state', { cache: 'no-store' }).then(r => r.json());
    liveSources = d.sources || {};
    if (d.rev !== liveRev) {
      const prev = LIVE;
      LIVE = d.n || {};
      liveRev = d.rev;
      applyLive();
      flashChanges(prev, LIVE);
      if (sel && LIVE[sel]) renderLiveRow(sel);
    }
    paintLiveStatus(true);
    liveFails = 0;
  } catch (e) {
    paintLiveStatus(false);
    liveFails++;
  }
  // Back off when the server is unreachable instead of retrying every 3s
  // forever — a dead endpoint should go quiet, not fill the console.
  again(liveFails ? Math.min(30000, 3000 * 2 ** (liveFails - 1)) : 3000);
}

/** A brief pulse on whatever just changed, so you notice it from across the board. */
function flashChanges(prev, now) {
  for (const id in now) {
    const a = prev[id], b = now[id];
    if (!a || a.b !== b.b || a.a !== b.a || a.h !== b.h) {
      const el = EL.get(id);
      if (!el) continue;
      el.classList.remove('flash');
      void el.offsetWidth;                 // restart the animation
      el.classList.add('flash');
    }
  }
}

function paintLiveStatus(ok) {
  const dot = $('#livedot');
  if (!dot) return;
  const bad = Object.entries(liveSources).filter(([, v]) => !v.ok).map(([k]) => k);
  dot.className = 'livedot' + (ok && !bad.length ? ' on' : ok ? ' partial' : ' off');
  dot.title = ok
    ? (bad.length ? 'live, but not reading: ' + bad.join(', ') : 'live — Home Assistant, Docker and reachability')
    : 'the map is not receiving live state';
}

function renderLiveRow(id) {
  const box = $('#insp .livebox');
  if (!box) return;
  const s = LIVE[id];
  box.innerHTML = s
    ? `<b>${esc(s.b || (s.a ? 'on' : 'off'))}</b>${s.d ? ' · ' + esc(s.d) : ''}${s.h ? ` · ${esc(s.h)}` : ''}`
    : '<span style="color:var(--ink-3)">not tracked live</span>';
}

document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') pollLive(); });

/* ---------------------------------------------------------------- filter */
function applyFilter() {
  const q = query.trim().toLowerCase();
  const lit = new Set();
  if (sel) { lit.add(sel); for (const p of svg.children) { const a = p.dataset.a, b = p.dataset.b; if (a === sel) lit.add(b); if (b === sel) lit.add(a); } }
  for (const [id, el] of EL) {
    const n = N.get(id);
    if (!n) continue;              // element not cleaned up yet — never throw over it
    const hay = (label(n) + ' ' + (n.sub || '') + ' ' + id + ' ' + JSON.stringify(n.meta || {})).toLowerCase();
    const isHit = q && hay.includes(q);
    el.classList.toggle('hit', !!isHit);
    el.classList.toggle('dim', (q && !isHit && !isGroup(id)) || (sel && !lit.has(id) && !isGroup(id) && !q));
    el.classList.toggle('sel', id === sel);
  }
  for (const p of svg.children) {
    const on = sel && (p.dataset.a === sel || p.dataset.b === sel);
    p.classList.toggle('lit', !!on);
    p.classList.toggle('dim', !!(sel && !on));
  }
}

/* ----------------------------------------------------------- reparenting */
/** Deepest group whose box contains `pt`, never the dragged node itself or
 *  anything inside it (that would make a loop). */
function groupAt(pt, exclude) {
  let best = null, bestDepth = -1;
  for (const [id, el] of EL) {
    if (el.dataset.kind !== 'g') continue;
    if (id === exclude || ancestorOf(exclude, id)) continue;
    const r = rect(id);
    if (pt.x < r.x || pt.x > r.x + r.w || pt.y < r.y || pt.y > r.y + r.h) continue;
    let d = 0;
    for (let c = eff(id); c; c = eff(c)) d++;
    if (d > bestDepth) { best = id; bestDepth = d; }
  }
  return best;
}

function pointerWorld(e) {
  const r = vp.getBoundingClientRect();
  return { x: (e.clientX - r.left - view.x) / view.z, y: (e.clientY - r.top - view.y) / view.z };
}

function setDropHint(id) {
  for (const [k, el] of EL) el.classList.toggle('droptarget', k === id);
}

/** Move a node into a group (or out to the top level) without letting it jump:
 *  its stored position is relative to the parent, so it is recomputed from the
 *  world position it already had. */
function reparent(id, tgt) {
  const before = abs(id);
  OV.parents = OV.parents || {};
  if (tgt) OV.parents[id] = tgt; else OV.parents[id] = null;
  index(); relayout();
  const base = tgt ? abs(tgt) : { x: 0, y: 0 };
  OV.positions[id] = { x: before.x - base.x, y: before.y - base.y };
  relayout(); save();
}

/* ------------------------------------------------------------ interaction */
let drag = null, pan = null;

vp.addEventListener('pointerdown', e => {
  const el = e.target.closest('.node, .group');
  const onHead = e.target.closest('.ghead');
  if (el && (el.dataset.kind === 'n' || onHead) && e.button === 0) {
    const id = el.dataset.id;
    if (linking) { finishLink(id); return; }
    select(id);
    drag = { id, sx: e.clientX, sy: e.clientY, start: Object.assign({}, REL.get(id) || { x: 0, y: 0 }), moved: false };
    capture(vp, e.pointerId);
    e.preventDefault();
    return;
  }
  if (e.button === 0 || e.button === 1) {
    pan = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
    vp.classList.add('panning');
    capture(vp, e.pointerId);
  }
});

vp.addEventListener('pointermove', e => {
  if (drag) {
    const dx = (e.clientX - drag.sx) / view.z, dy = (e.clientY - drag.sy) / view.z;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    REL.set(drag.id, { x: drag.start.x + dx, y: drag.start.y + dy });
    OV.positions[drag.id] = REL.get(drag.id);
    if (drag.moved) setDropHint(groupAt(pointerWorld(e), drag.id));
    relayoutSoon();
  } else if (pan) {
    view.x = pan.vx + (e.clientX - pan.sx); view.y = pan.vy + (e.clientY - pan.sy);
    if (Math.abs(e.clientX - pan.sx) + Math.abs(e.clientY - pan.sy) > 3) pan.moved = true;
    applyView(); drawMini();
  }
});

vp.addEventListener('pointerup', e => {
  if (drag) {
    if (drag.moved) {
      const id = drag.id, tgt = groupAt(pointerWorld(e), id);
      setDropHint(null);
      if ((tgt || null) !== (eff(id) || null)) {
        reparent(id, tgt);
        const tn = tgt && N.get(tgt);
        toast(tgt ? 'Moved into ' + label(tn) : 'Moved out to the top level');
      } else save();
    }
    drag = null;
  }
  else if (pan) { if (!pan.moved && !e.target.closest('.node,.group')) select(null); pan = null; saveViewSoon(); }
  vp.classList.remove('panning');
});

let raf = 0;
function relayoutSoon() {
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; BOX.clear(); ABS.clear();
    const roots = kidsOf(''); for (const r of roots) computeBox(r.id); render(); });
}

vp.addEventListener('wheel', e => {
  e.preventDefault();
  const r = vp.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  if (e.ctrlKey || !e.shiftKey) {
    const f = Math.exp(-e.deltaY * .0015);
    const z = Math.min(3, Math.max(.06, view.z * f));
    view.x = mx - (mx - view.x) * (z / view.z);
    view.y = my - (my - view.y) * (z / view.z);
    view.z = z;
  } else { view.x -= e.deltaX; view.y -= e.deltaY; }
  applyView(); drawMini(); saveViewSoon();
}, { passive: false });

vp.addEventListener('dblclick', e => {
  const el = e.target.closest('.node, .group');
  if (!el) return;
  const id = el.dataset.id;
  if (!(KIDS.get(id) || []).length) return;
  const i = OV.collapsed.indexOf(id);
  if (i >= 0) OV.collapsed.splice(i, 1); else OV.collapsed.push(id);
  relayout(); save();
});

document.addEventListener('keydown', e => {
  if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
  else if (e.key === 'f' || e.key === 'F') fit();
  else if (e.key === 'Escape') { linking = false; vp.classList.remove('linking'); select(null); $('#help').classList.remove('open'); }
  else if (e.key === '?') $('#help').classList.toggle('open');
  else if ((e.key === 'Delete' || e.key === 'Backspace') && sel) toggleHide(sel);
});

/* -------------------------------------------------------------- selection */
function select(id) {
  sel = id;
  applyFilter();
  const insp = $('#insp');
  if (!id) { insp.classList.remove('open'); return; }
  insp.classList.add('open');
  renderInspector(N.get(id));
}

function renderInspector(n) {
  const cust = (OV.customNodes || []).some(c => c.id === n.id);
  const kids = (KIDS.get(n.id) || []).length;
  const meta = n.meta || {};
  const links = G.edges.concat(OV.customEdges || []).filter(e => e.from === n.id || e.to === n.id);
  $('#insp header').innerHTML =
    `<span class="ic">${esc(icon(n))}</span><div style="min-width:0"><h2>${esc(label(n))}</h2>` +
    `<div class="sub">${esc(n.type)}${n.sub ? ' · ' + esc(n.sub) : ''}</div></div>` +
    `<button class="close" title="Close (Esc)">×</button>`;
  $('#insp header .close').onclick = () => select(null);

  let h = '';
  h += `<div class="f"><label>Right now</label><div class="livebox"></div></div>`;
  if (n.trap) h += `<div class="trap"><b>⚠ Known trap</b>${esc(n.trap)}</div>`;
  if (n.url) h += `<div class="f"><label>Open</label><a class="linkout" href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.url)}</a></div>`;

  h += `<h3 class="sec">Yours</h3>`;
  h += `<div class="f"><label>Name</label><input type="text" id="i-label" value="${esc(label(n))}"></div>`;
  h += `<div class="f"><label>Icon</label><div class="chips" id="i-icons">` +
    ['🖥','🗄','🏠','📦','🐳','💡','📡','🎮','🎵','🧠','🔒','💾','📱','📺','⚙','🔌','🌐','⭐','📝','🚨']
      .map(x => `<span class="chip">${x}</span>`).join('') +
    `<span class="chip" data-clear="1">reset</span></div></div>`;
  h += `<div class="f"><label>Accent</label><div class="swatches" id="i-acc">` +
    ['#3987e5', '#d95926', '#199e70', '#fab219', '#d03b3b', '#9085e9']
      .map(c => `<span class="sw" data-c="${c}" style="background:${c}"></span>`).join('') +
    `<span class="sw none" data-c="">×</span></div></div>`;
  h += `<div class="f"><label>Notes</label><textarea id="i-note" placeholder="Anything you want to remember about this…">${esc(OV.notes[n.id] || '')}</textarea></div>`;
  h += `<div class="f row"><button class="btn sm" id="b-link">Draw a link…</button>` +
    `<button class="btn sm" id="b-hide">${hidden(n.id) ? 'Unhide' : 'Hide'}</button>` +
    (kids ? `<button class="btn sm" id="b-col">${collapsed(n.id) ? 'Expand' : 'Collapse'}</button>` : '') +
    (eff(n.id) ? `<button class="btn sm" id="b-out">Move out</button>` : '') +
    (OV.positions[n.id] ? `<button class="btn sm" id="b-reset">Reset position</button>` : '') +
    (cust ? `<button class="btn sm" id="b-del">Delete</button>` : '') + `</div>`;

  if (Object.keys(meta).length) {
    h += `<h3 class="sec">Scanned</h3><dl class="kv">` +
      Object.entries(meta).filter(([, v]) => v !== '' && v != null)
        .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('') + `</dl>`;
  }
  if (links.length) {
    h += `<h3 class="sec">Connections (${links.length})</h3><div class="chips">` +
      links.map(e => { const o = e.from === n.id ? e.to : e.from; const on = N.get(o);
        return `<span class="chip" data-goto="${esc(o)}">${e.from === n.id ? '→ ' : '← '}${esc(on ? label(on) : o)}${e.label ? ' · ' + esc(e.label) : ''}</span>`;
      }).join('') + `</div>`;
  }
  const par = eff(n.id) && N.get(eff(n.id));
  h += `<h3 class="sec">Identity</h3><dl class="kv"><dt>id</dt><dd>${esc(n.id)}</dd>` +
    (par ? `<dt>inside</dt><dd>${esc(label(par))}</dd>` : '') +
    `<dt>layer</dt><dd>${esc(n.layer || '—')}</dd>${kids ? `<dt>children</dt><dd>${kids}</dd>` : ''}</dl>`;

  const body = $('#insp .body');
  body.innerHTML = h; body.scrollTop = 0;
  renderLiveRow(n.id);

  $('#i-label').oninput = e => { setOV(OV.labels, n.id, e.target.value, n.label); render(); saveSoon(); };
  $('#i-note').oninput = e => { setOV(OV.notes, n.id, e.target.value, ''); saveSoon(); };
  $('#i-icons').onclick = e => {
    const c = e.target.closest('.chip'); if (!c) return;
    if (c.dataset.clear) delete OV.icons[n.id]; else OV.icons[n.id] = c.textContent;
    render(); renderInspector(n); save();
  };
  $('#i-acc').onclick = e => {
    const s = e.target.closest('.sw'); if (!s) return;
    if (s.dataset.c) OV.accents[n.id] = s.dataset.c; else delete OV.accents[n.id];
    render(); save();
  };
  const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  on('#b-link', () => { linking = n.id; vp.classList.add('linking'); toast('Click the node to link to — Esc cancels'); });
  on('#b-hide', () => toggleHide(n.id));
  on('#b-col', () => { const i = OV.collapsed.indexOf(n.id); if (i >= 0) OV.collapsed.splice(i, 1); else OV.collapsed.push(n.id); relayout(); renderInspector(n); save(); });
  on('#b-out', () => { reparent(n.id, null); renderInspector(n); });
  on('#b-reset', () => { delete OV.positions[n.id]; relayout(); renderInspector(n); save(); });
  on('#b-del', () => {
    const gone = n.id;
    // Anything inside a deleted group comes back out to the top level.
    for (const k of (KIDS.get(gone) || [])) if (OV.parents) delete OV.parents[k.id];
    OV.customNodes = (OV.customNodes || []).filter(c => c.id !== gone);
    forgetNode(gone);
    sel = null;
    index();
    relayout();          // clears the element BEFORE anything reads N for it
    select(null);
    save();
    toast('Deleted');
  });
  body.querySelectorAll('[data-goto]').forEach(c => c.onclick = () => focusOn(c.dataset.goto));
}

const OV_MAPS = ['positions', 'labels', 'icons', 'accents', 'notes', 'parents'];

/** Drop every trace of a node from the overrides, so deleting leaves nothing
 *  behind (earlier versions left orphan `hidden` and `positions` entries). */
function forgetNode(id) {
  for (const k of OV_MAPS) if (OV[k]) delete OV[k][id];
  OV.hidden = (OV.hidden || []).filter(x => x !== id);
  OV.collapsed = (OV.collapsed || []).filter(x => x !== id);
  OV.customEdges = (OV.customEdges || []).filter(e => e.from !== id && e.to !== id);
  if (OV.parents) for (const k of Object.keys(OV.parents)) if (OV.parents[k] === id) delete OV.parents[k];
}

/** Clear overrides pointing at custom nodes that no longer exist.
 *  Deliberately limited to `my:` ids — a scanned id missing because a scan
 *  failed must keep its layout, or a bad scan would wipe your arrangement. */
function pruneOrphans() {
  const alive = new Set((OV.customNodes || []).map(n => n.id));
  const stale = id => typeof id === 'string' && id.startsWith('my:') && !alive.has(id);
  for (const k of OV_MAPS) if (OV[k]) for (const id of Object.keys(OV[k])) if (stale(id)) delete OV[k][id];
  OV.hidden = (OV.hidden || []).filter(id => !stale(id));
  OV.collapsed = (OV.collapsed || []).filter(id => !stale(id));
  OV.customEdges = (OV.customEdges || []).filter(e => !stale(e.from) && !stale(e.to));
}

function setOV(bag, id, val, dflt) { if (!val || val === dflt) delete bag[id]; else bag[id] = val; }

function toggleHide(id) {
  const i = OV.hidden.indexOf(id);
  if (i >= 0) OV.hidden.splice(i, 1); else { OV.hidden.push(id); if (sel === id) select(null); }
  relayout(); save();
}

function finishLink(to) {
  if (linking && linking !== to) {
    const lbl = prompt('Label for this link (optional):', '') ?? '';
    OV.customEdges.push({ from: linking, to, kind: 'custom', label: lbl });
    save(); toast('Link added');
  }
  linking = false; vp.classList.remove('linking'); relayout();
}

function focusOn(id) {
  let cur = N.get(id);
  while (cur && cur.parent) { const i = OV.collapsed.indexOf(cur.parent); if (i >= 0) OV.collapsed.splice(i, 1); cur = N.get(cur.parent); }
  relayout();
  const r = rect(id); if (!r) return;
  const b = vp.getBoundingClientRect();
  view.z = Math.min(1.1, view.z < .5 ? .8 : view.z);
  view.x = b.width / 2 - (r.x + r.w / 2) * view.z;
  view.y = b.height / 2 - (r.y + r.h / 2) * view.z;
  applyView(); select(id); drawMini(); saveViewSoon();
}

function worldBounds() {
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const id of EL.keys()) { const r = rect(id); x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y); x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h); }
  if (x0 > x1) return { x: 0, y: 0, w: 1000, h: 800 };
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

function fit() {
  const b = worldBounds(), r = vp.getBoundingClientRect();
  const pad = 50;
  view.z = Math.min(1.4, Math.min((r.width - pad * 2) / b.w, (r.height - pad * 2) / b.h));
  view.x = (r.width - b.w * view.z) / 2 - b.x * view.z;
  view.y = (r.height - b.h * view.z) / 2 - b.y * view.z;
  applyView(); drawMini(); saveViewSoon();
}

/* ------------------------------------------------------------------ minimap */
function drawMini() {
  const c = $('#mini'), ctx = c.getContext('2d');
  const w = c.width = c.clientWidth * 2, h = c.height = c.clientHeight * 2;
  ctx.clearRect(0, 0, w, h);
  const b = worldBounds(), s = Math.min(w / (b.w * 1.05), h / (b.h * 1.05));
  const ox = (w - b.w * s) / 2 - b.x * s, oy = (h - b.h * s) / 2 - b.y * s;
  const css = getComputedStyle(document.documentElement);
  for (const id of EL.keys()) {
    const n = N.get(id), r = rect(id);
    if (!n) continue;
    const g = EL.get(id).dataset.kind === 'g';
    ctx.fillStyle = g ? css.getPropertyValue('--line').trim() : (n.status === 'down' ? css.getPropertyValue('--crit').trim() : css.getPropertyValue('--ink-3').trim());
    ctx.globalAlpha = g ? .5 : .9;
    ctx.fillRect(r.x * s + ox, r.y * s + oy, Math.max(1.5, r.w * s), Math.max(1.5, r.h * s));
  }
  ctx.globalAlpha = 1;
  const vr = vp.getBoundingClientRect();
  ctx.strokeStyle = css.getPropertyValue('--c1').trim(); ctx.lineWidth = 2;
  ctx.strokeRect((-view.x / view.z) * s + ox, (-view.y / view.z) * s + oy, (vr.width / view.z) * s, (vr.height / view.z) * s);
  c._m = { s, ox, oy };
}
$('#mini').addEventListener('click', e => {
  const c = $('#mini'), m = c._m; if (!m) return;
  const r = c.getBoundingClientRect();
  const wx = ((e.clientX - r.left) * 2 - m.ox) / m.s, wy = ((e.clientY - r.top) * 2 - m.oy) / m.s;
  const vr = vp.getBoundingClientRect();
  view.x = vr.width / 2 - wx * view.z; view.y = vr.height / 2 - wy * view.z;
  applyView(); drawMini(); saveViewSoon();
});

/* ------------------------------------------------------------------ chrome */
function buildLayerBar() {
  const layers = [...new Set(G.nodes.map(n => n.layer).filter(Boolean))];
  /* The scan names its own layers — nothing about one particular lab is
     baked in here. Falls back to the id so a new layer still gets a button. */
  const names = G.layers || {};
  $('#layers').innerHTML = layers.map(l => `<button class="btn sm on" data-layer="${l}">${names[l] || l}</button>`).join('');
  $('#layers').onclick = e => {
    const b = e.target.closest('[data-layer]'); if (!b) return;
    const l = b.dataset.layer;
    if (layersOff.has(l)) layersOff.delete(l); else layersOff.add(l);
    b.classList.toggle('on', !layersOff.has(l));
    relayout();
  };
}

$('#search').oninput = e => { query = e.target.value; applyFilter(); };
$('#search').onkeydown = e => {
  if (e.key !== 'Enter') return;
  const q = query.trim().toLowerCase();
  const hit = G.nodes.concat(OV.customNodes).find(n => (label(n) + ' ' + (n.sub || '') + ' ' + n.id).toLowerCase().includes(q));
  if (hit) focusOn(hit.id);
};
$('#b-fit').onclick = fit;
$('#b-help').onclick = () => $('#help').classList.toggle('open');
$('#help').onclick = e => { if (e.target.id === 'help' || e.target.dataset.close) $('#help').classList.remove('open'); };
$('#b-theme').onclick = () => {
  const d = document.documentElement;
  d.dataset.theme = d.dataset.theme === 'light' ? 'dark' : 'light';
  OV.theme = d.dataset.theme; drawMini(); renderExcal(); save();
};
$('#b-hidden').onclick = e => { showHidden = !showHidden; e.target.classList.toggle('on', showHidden); relayout(); };
$('#b-add').onclick = () => {
  const name = prompt('Name for the new node:'); if (!name) return;
  const r = vp.getBoundingClientRect();
  const id = 'my:' + Date.now().toString(36);
  OV.customNodes.push({ id, type: 'note', label: name, sub: '', icon: '📝', layer: '', parent: null, meta: {} });
  OV.positions[id] = { x: (r.width / 2 - view.x) / view.z, y: (r.height / 2 - view.y) / view.z };
  index(); relayout(); select(id); save();
};
$('#b-group').onclick = () => {
  const name = prompt('Name for the new group:');
  if (!name) return;
  const r = vp.getBoundingClientRect();
  const id = 'my:' + Date.now().toString(36);
  OV.customNodes.push({ id, type: 'group', label: name, sub: '', icon: '\u{1F4C1}',
                        layer: '', parent: null, meta: {} });
  OV.positions[id] = { x: (r.width / 2 - view.x) / view.z, y: (r.height / 2 - view.y) / view.z };
  index(); relayout(); select(id); save();
  toast('Drag nodes into it');
};

$('#b-collapse').onclick = () => {
  const groups = [];
  (function w(p) { for (const k of kidsOf(p)) if ((KIDS.get(k.id) || []).length) { groups.push(k.id); w(k.id); } })('');
  const tops = kidsOf('').filter(n => (KIDS.get(n.id) || []).length).map(n => n.id);
  // Folds every nested group but leaves the top-level regions open, so the
  // board stays readable. Pressing it again opens everything back up.
  const nested = groups.filter(id => !tops.includes(id));
  OV.collapsed = nested.length && nested.every(collapsed) ? [] : nested;
  relayout(); save();
};

/* ------------------------------------------------------------------ saving */
let saveT = 0, saving = false, savePending = false;
function saveSoon() { clearTimeout(saveT); saveT = setTimeout(save, 700); }
function saveViewSoon() { OV.view = view; saveSoon(); }

/* Every save PUTs the whole document, so two in flight at once can land out of
 * order and quietly undo the newer one. Only ever keep one in flight; if more
 * changes arrive meanwhile, re-send afterwards from the current state. */
async function save() {
  clearTimeout(saveT);
  OV.view = view;
  if (saving) { savePending = true; return; }
  saving = true;
  try {
    const r = await fetch('api/overrides', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(OV) });
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (err) {
    toast('Could not save your changes — ' + err.message);
  } finally {
    saving = false;
    if (savePending) { savePending = false; await save(); }
  }
}
function toast(m) { const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 2600); }

addEventListener('resize', () => { drawMini(); });
boot().catch(e => { document.body.innerHTML = `<div style="padding:40px;font:14px system-ui">Could not load the map: ${esc(e.message)}</div>`; });
