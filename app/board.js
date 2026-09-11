// spritz board — infinite wall, build-free. All rendering goes through el()/textContent,
// no innerHTML, so stored strings (titles, prompts, notes) never execute as markup.
'use strict';
const $ = s => document.querySelector(s);
const api = (url, opts) => fetch(url, opts).then(r => { if (!r.ok) throw new Error(r.status + ' ' + url); return r.json(); });
const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const uid = () => Math.random().toString(16).slice(2, 10);
const HEAD = 25;                                  // card header height, px
const isMedia = /^(image|video|audio)$/;

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n[k] = v;
    else if (k === 'style') n.style.cssText = v;
    else if (v !== false && v != null) n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}
function replace(node, ...children) { node.replaceChildren(...children.filter(c => c != null)); }

// ---------------------------------------------------------------- state
let PID = new URLSearchParams(location.search).get('project') || localStorage.getItem('spritz.project') || null;
let B = {cards: [], edges: [], updated: 0};
let META = {models: [], camera_moves: []};
let STATE = {projects: {}};
const view = {x: 0, y: 0, s: 1};
const sel = new Set();
const mounted = new Map();                        // card id -> element
let undoStack = [], redoStack = [];
let sse = null, saveTimer = null, dirty = false, saving = false, pendingPoll = null;
let maxZ = 0;

const canvas = $('#canvas'), world = $('#world'), cardsLayer = $('#cards'), edgesSvg = $('#edges');
const byId = id => B.cards.find(c => c.id === id);
const subjects = () => B.cards.filter(c => c.type === 'subject');

// ---------------------------------------------------------------- boot
async function boot() {
  META = await api('/api/models');
  STATE = await api('/api/state');
  const ids = Object.keys(STATE.projects);
  if (!PID || !STATE.projects[PID]) PID = ids[0] || null;
  renderProjects();
  if (PID) await loadBoard();
  bindUI();
}

function renderProjects() {
  const s = $('#project-select');
  const opts = Object.values(STATE.projects).map(p => el('option', {value: p.id, text: p.name, selected: p.id === PID}));
  replace(s, ...(opts.length ? opts : [el('option', {text: '— no project —'})]));
}

async function loadBoard() {
  localStorage.setItem('spritz.project', PID);
  history.replaceState(null, '', '?project=' + PID);
  B = await api(`/api/projects/${PID}/board`);
  maxZ = Math.max(0, ...B.cards.map(c => c.z || 0));
  sel.clear(); undoStack = []; redoStack = [];
  for (const e of mounted.values()) e.remove();
  mounted.clear();
  fit();
  connectSSE();
  schedulePoll();
}

// ---------------------------------------------------------------- view
function applyView() {
  world.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.s})`;
  canvas.classList.toggle('grid', view.s > 0.5);
  canvas.style.setProperty('--gs', 24 * view.s + 'px');
  canvas.style.setProperty('--gx', view.x + 'px');
  canvas.style.setProperty('--gy', view.y + 'px');
  $('#zoom-label').textContent = Math.round(view.s * 100) + '%';
  render();
}
const toWorld = (sx, sy) => ({x: (sx - view.x) / view.s, y: (sy - view.y) / view.s});
function canvasPoint(e) { const r = canvas.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; }
function center() { return toWorld(canvas.clientWidth / 2, canvas.clientHeight / 2); }

function fit() {
  if (!B.cards.length) { view.x = canvas.clientWidth / 2; view.y = canvas.clientHeight / 2; view.s = 1; return applyView(); }
  const x0 = Math.min(...B.cards.map(c => c.x)), y0 = Math.min(...B.cards.map(c => c.y));
  const x1 = Math.max(...B.cards.map(c => c.x + c.w)), y1 = Math.max(...B.cards.map(c => c.y + c.h));
  const pad = 60;
  view.s = Math.min(2, Math.max(0.05, Math.min((canvas.clientWidth - pad * 2) / (x1 - x0), (canvas.clientHeight - pad * 2) / (y1 - y0))));
  view.x = (canvas.clientWidth - (x1 - x0) * view.s) / 2 - x0 * view.s;
  view.y = (canvas.clientHeight - (y1 - y0) * view.s) / 2 - y0 * view.s;
  applyView();
}
function zoomAt(sx, sy, factor) {
  const s = Math.min(4, Math.max(0.05, view.s * factor));
  view.x = sx - (sx - view.x) * (s / view.s);
  view.y = sy - (sy - view.y) * (s / view.s);
  view.s = s; applyView();
}

// ---------------------------------------------------------------- render (virtualised)
function render() {
  const m = 200 / view.s;
  const v = {x0: -view.x / view.s - m, y0: -view.y / view.s - m,
             x1: (canvas.clientWidth - view.x) / view.s + m, y1: (canvas.clientHeight - view.y) / view.s + m};
  const seen = new Set();
  for (const c of B.cards) {
    const vis = c.x < v.x1 && c.x + c.w > v.x0 && c.y < v.y1 && c.y + c.h > v.y0;
    if (!vis) continue;
    seen.add(c.id);
    let e = mounted.get(c.id);
    if (!e) { e = buildCard(c); mounted.set(c.id, e); cardsLayer.append(e); }
    place(c, e);
  }
  for (const [id, e] of mounted) if (!seen.has(id)) { e.remove(); mounted.delete(id); }
  drawEdges();
  $('#empty').hidden = B.cards.length > 0;
}
function place(c, e) {
  e.style.left = c.x + 'px'; e.style.top = c.y + 'px';
  e.style.width = c.w + 'px'; e.style.height = c.h + 'px';
  e.style.zIndex = c.type === 'group' ? 0 : 1 + (c.z || 0);
  e.classList.toggle('selected', sel.has(c.id));
}
function refreshCard(id) {
  const old = mounted.get(id); if (!old) return;
  const c = byId(id); if (!c) { old.remove(); mounted.delete(id); return; }
  const e = buildCard(c); old.replaceWith(e); mounted.set(id, e); place(c, e);
}
function drawEdges() {
  const paths = [];
  for (const e of B.edges) {
    const a = byId(e.from), b = byId(e.to); if (!a || !b) continue;
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    const dx = Math.max(40, Math.abs(x2 - x1) / 2);
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
    p.setAttribute('class', e.kind || 'note');
    paths.push(p);
  }
  replace(edgesSvg, ...paths);
}

// ---------------------------------------------------------------- cards
function buildCard(c) {
  const e = el('div', {class: 'card ' + c.type, 'data-id': c.id});
  const title = el('input', {class: 'title', value: c.title || '', placeholder: c.type,
    onchange: ev => { snapshot(); c.title = ev.target.value; touch(c); if (c.type === 'subject') refreshScenes(); }});
  e.append(el('div', {class: 'head'}, el('span', {class: 'kind', text: c.type}), title));
  const body = el('div', {class: 'body'});
  e.append(body);
  const d = c.data || (c.data = {});
  const build = BUILDERS[c.type] || BUILDERS.file;
  build(c, d, body, e);
  if (c.type !== 'group') e.append(el('div', {class: 'grip'}));
  else e.append(el('div', {class: 'grip'}));
  return e;
}

const mediaSrc = (d, k) => d[k] || null;
const BUILDERS = {
  text(c, d, body) {
    const ta = el('textarea', {placeholder: 'Write…', oninput: ev => { d.text = ev.target.value; touch(c, true); }});
    ta.value = d.text || '';
    ta.onfocus = () => snapshot();
    body.append(ta);
  },
  image(c, d, body) {
    const src = mediaSrc(d, 'thumb') || d.asset;
    if (src) {
      const img = el('img', {src, draggable: false, alt: c.title || ''});
      img.onerror = () => { if (img.src !== location.origin + d.asset) img.src = d.asset; };
      body.append(img);
      body.onclick = ev => { if (!dragMoved) lightbox(el('img', {src: d.asset})); };
    } else body.append(el('div', {class: 'ph', text: 'uploading…'}));
  },
  video(c, d, body) {
    if (d.proxy) {
      const v = el('video', {src: d.proxy, muted: true, loop: true, preload: 'metadata', playsinline: true});
      v.muted = true;
      v.onmousemove = ev => { if (v.paused && v.duration) { const r = v.getBoundingClientRect(); v.currentTime = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * v.duration; } };
      v.onclick = () => { if (dragMoved) return; v.paused ? v.play() : v.pause(); };
      v.ondblclick = () => lightbox(el('video', {src: d.proxy, controls: true, autoplay: true, loop: true}));
      body.append(v);
    } else if (d.thumb) {
      body.append(el('img', {src: d.thumb, draggable: false}), el('div', {class: 'badge', text: 'encoding proxy…'}));
    } else body.append(el('div', {class: 'ph', text: d.asset ? 'encoding proxy…' : 'uploading…'}));
    if (d.duration) body.append(el('div', {class: 'dur', text: fmtDur(d.duration)}));
  },
  audio(c, d, body) {
    body.append(d.wave ? el('img', {src: d.wave, draggable: false}) : el('div', {class: 'ph', text: d.asset ? 'waveform…' : 'uploading…'}));
    if (d.asset) body.append(el('audio', {src: d.asset, controls: true, preload: 'none'}));
  },
  pdf(c, d, body) {
    if (d.thumb) body.append(el('img', {src: d.thumb, draggable: false}));
    else body.append(el('div', {class: 'ph', text: 'PDF'}));
    body.onclick = () => { if (!dragMoved && d.asset) window.open(d.asset, '_blank'); };
  },
  file(c, d, body) {
    body.append(el('div', {class: 'ph'}, el('span', {text: (d.asset || '').split('/').pop() || 'file'})));
    body.onclick = () => { if (!dragMoved && d.asset) window.open(d.asset, '_blank'); };
  },
  link(c, d, body) {
    if (!d.url) {
      const inp = el('input', {placeholder: 'https://…', onchange: ev => { snapshot(); setLink(c, ev.target.value); refreshCard(c.id); }});
      body.append(inp); setTimeout(() => inp.focus(), 0);
      return;
    }
    let host = d.url; try { host = new URL(d.url).hostname; } catch {}
    const fav = el('img', {src: d.favicon || '', draggable: false, alt: ''}); fav.onerror = () => fav.remove();
    body.append(el('div', {style: 'display:flex;gap:6px;align-items:center'}, fav, el('strong', {text: d.title || host})),
                el('a', {href: d.url, target: '_blank', rel: 'noopener', text: d.url}));
  },
  subject(c, d, body, e) {
    body.append(el('div', {class: 'alias', text: '@' + (c.title || 'subject').replace(/\s+/g, '_')}));
    const ta = el('textarea', {placeholder: 'Description (who, what, look)', oninput: ev => { d.description = ev.target.value; touch(c, true); }});
    ta.value = d.description || ''; ta.onfocus = () => snapshot();
    body.append(ta);
    const refs = el('div', {class: 'refs'});
    const list = d.refs || [];
    if (!list.length) refs.append(el('div', {class: 'hint', text: 'drop reference images here'}));
    for (const r of list) {
      const img = el('img', {src: r + '.thumb.jpg', draggable: false, title: 'click to remove'});
      img.onerror = () => { if (!img.src.endsWith(r)) img.src = r; };
      img.onclick = ev => { ev.stopPropagation(); if (!confirm('Remove this reference from the subject? The file stays on disk.')) return; snapshot(); d.refs = list.filter(x => x !== r); touch(c); refreshCard(c.id); };
      refs.append(img);
    }
    body.append(refs);
    e.ondragover = ev => { ev.preventDefault(); ev.stopPropagation(); e.classList.add('dropping'); };
    e.ondragleave = () => e.classList.remove('dropping');
    e.ondrop = async ev => {
      ev.preventDefault(); ev.stopPropagation(); e.classList.remove('dropping');
      const files = [...ev.dataTransfer.files].filter(f => f.type.startsWith('image/'));
      if (!files.length) return;
      snapshot();
      for (const f of files) { const r = await upload(f); (d.refs = d.refs || []).push(r.asset); }
      touch(c); refreshCard(c.id);
    };
  },
  scene(c, d, body) {
    const ta = el('textarea', {placeholder: 'Prompt — reference subjects as @name', oninput: ev => { d.prompt = ev.target.value; touch(c, true); }});
    ta.value = d.prompt || ''; ta.onfocus = () => snapshot();
    body.append(ta);
    const model = el('select', {onchange: ev => { snapshot(); d.model = ev.target.value; touch(c); }},
      ...META.models.map(m => el('option', {value: m.id, text: m.name, selected: m.id === (d.model || 'mock')})));
    const cam = el('select', {onchange: ev => { snapshot(); d.camera = ev.target.value; touch(c); }},
      ...META.camera_moves.map(m => el('option', {value: m, text: m, selected: m === (d.camera || 'static')})));
    const dur = el('input', {type: 'number', min: 1, max: 15, value: d.duration || 5, title: 'seconds',
      onchange: ev => { snapshot(); d.duration = +ev.target.value; touch(c); }});
    body.append(el('div', {class: 'row'}, model, dur), el('div', {class: 'row'}, cam));
    // references = edges kind "references" from this scene to subject cards
    const refs = el('div', {class: 'refs'});
    const linked = B.edges.filter(e => e.from === c.id && e.kind === 'references').map(e => byId(e.to)).filter(Boolean);
    for (const s of linked) refs.append(el('span', {class: 'tag', text: '@' + (s.title || 'subject').replace(/\s+/g, '_'), title: 'click to unlink',
      onclick: () => { snapshot(); B.edges = B.edges.filter(e => !(e.from === c.id && e.to === s.id)); touch(c); refreshCard(c.id); }}));
    const free = subjects().filter(s => !linked.includes(s));
    if (free.length) refs.append(el('select', {onchange: ev => { if (!ev.target.value) return; snapshot(); B.edges.push({from: c.id, to: ev.target.value, kind: 'references'}); touch(c); refreshCard(c.id); }},
      el('option', {value: '', text: '+ reference'}), ...free.map(s => el('option', {value: s.id, text: s.title || s.id}))));
    else if (!linked.length) refs.append(el('span', {style: 'color:var(--t3)', text: 'no subjects on the wall'}));
    body.append(refs);
    const st = el('span', {class: 'st', text: d.takes ? `${d.takes} take${d.takes > 1 ? 's' : ''}` : ''});
    const btn = el('button', {class: 'primary', text: 'Generate', onclick: async () => {
      btn.disabled = true; st.textContent = 'queued…';
      try {
        await flushSave();                                 // the shot must exist server-side first
        const job = await api(`/api/projects/${PID}/shots/${c.id}/generate`, {method: 'POST'});
        st.textContent = `running ${job.id}…`;
        d.job = job.id;
        watchJob(job.id, st, btn);
      } catch (err) { st.textContent = 'error: ' + err.message; btn.disabled = false; }
    }});
    body.append(el('div', {class: 'gen'}, st, btn));
  },
  take(c, d, body) {
    const src = d.proxy || d.output;
    if (d.kind === 'video' && src) {
      const v = el('video', {src, muted: true, loop: true, preload: 'metadata', playsinline: true}); v.muted = true;
      v.onmousemove = ev => { if (v.paused && v.duration) { const r = v.getBoundingClientRect(); v.currentTime = ((ev.clientX - r.left) / r.width) * v.duration; } };
      v.onclick = () => { if (dragMoved) return; v.paused ? v.play() : v.pause(); };
      body.append(v);
    } else if (d.kind === 'image' && src) {
      body.append(el('img', {src: d.thumb || src, draggable: false}));
      body.onclick = () => { if (!dragMoved) lightbox(el('img', {src: d.output})); };
    } else body.append(el('div', {class: 'note', text: (d.note ? d.note + '\n' : '') + (d.output || '')}));
    body.append(el('div', {class: 'badge', text: [d.model, d.seed != null ? 'seed ' + d.seed : null, d.job].filter(Boolean).join(' · ')}));
  },
  group() {},
};

async function watchJob(jid, st, btn) {
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 1500));
    const s = await api('/api/state');
    const j = s.jobs[jid]; if (!j) return;
    if (j.status === 'done') { st.textContent = 'done'; btn.disabled = false; return; }
    if (j.status === 'error') { st.textContent = 'error: ' + (j.error || '').slice(0, 80); btn.disabled = false; return; }
  }
}
function setLink(c, url) {
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  let origin = ''; try { origin = new URL(url).origin; } catch { return; }
  c.data.url = url; c.data.favicon = origin + '/favicon.ico';
  if (!c.title) c.title = new URL(url).hostname;
  touch(c);
}
function refreshScenes() { for (const c of B.cards) if (c.type === 'scene' && mounted.has(c.id)) refreshCard(c.id); }
function fmtDur(s) { const m = Math.floor(s / 60), r = s - m * 60; return m ? `${m}:${r.toFixed(1).padStart(4, '0')}` : r.toFixed(1) + 's'; }

function lightbox(node) {
  const lb = $('#lightbox'); replace(lb, node); lb.hidden = false;
  lb.onclick = () => { lb.hidden = true; replace(lb); };
}

// ---------------------------------------------------------------- mutations, undo, save
function snapshot() {
  undoStack.push(JSON.stringify({cards: B.cards, edges: B.edges}));
  if (undoStack.length > 20) undoStack.shift();
  redoStack = [];
}
function restore(s) {
  const o = JSON.parse(s); B.cards = o.cards; B.edges = o.edges;
  for (const id of [...sel]) if (!byId(id)) sel.delete(id);
  for (const e of mounted.values()) e.remove(); mounted.clear();
  render(); markDirty();
}
function undo() { if (!undoStack.length) return; redoStack.push(JSON.stringify({cards: B.cards, edges: B.edges})); restore(undoStack.pop()); }
function redo() { if (!redoStack.length) return; undoStack.push(JSON.stringify({cards: B.cards, edges: B.edges})); restore(redoStack.pop()); }

function touch(c, quiet) { c.updated = Date.now() / 1000; markDirty(); if (!quiet) drawEdges(); }
function markDirty() {
  dirty = true; setSave('dirty');
  clearTimeout(saveTimer); saveTimer = setTimeout(flushSave, 500);
}
async function flushSave() {
  clearTimeout(saveTimer);
  if (!dirty || !PID) return;
  if (saving) { await new Promise(r => setTimeout(r, 200)); return flushSave(); }
  saving = true; dirty = false; setSave('saving');
  try {
    const r = await api(`/api/projects/${PID}/board`, json('PUT', {cards: B.cards, edges: B.edges}));
    B.updated = r.updated; setSave('saved');
  } catch (e) { dirty = true; setSave('error'); console.error(e); }
  finally { saving = false; if (dirty) saveTimer = setTimeout(flushSave, 800); }
}
function setSave(s) { const n = $('#save-state'); n.className = 'save ' + s; n.textContent = {dirty: 'unsaved', saving: 'saving…', saved: 'saved', error: 'save failed'}[s]; }
window.addEventListener('beforeunload', () => { if (dirty) flushSave(); });

function addCard(type, at, extra = {}) {
  snapshot();
  const size = {text: [240, 160], subject: [280, 260], scene: [320, 300], group: [420, 320], link: [260, 110],
                image: [280, 220], video: [320, 205], audio: [300, 120], pdf: [220, 280], file: [220, 100]}[type] || [240, 160];
  const c = {id: uid(), type, x: Math.round(at.x - size[0] / 2), y: Math.round(at.y - size[1] / 2), w: size[0], h: size[1],
             z: ++maxZ, title: '', created: Date.now() / 1000, updated: Date.now() / 1000, data: {}, ...extra};
  B.cards.push(c); sel.clear(); sel.add(c.id); render(); markDirty();
  if (type === 'text' || type === 'link') setTimeout(() => mounted.get(c.id)?.querySelector('textarea,input:not(.title)')?.focus(), 0);
  return c;
}
function deleteSelected() {
  if (!sel.size) return;
  if (!confirm(`Delete ${sel.size} card${sel.size > 1 ? 's' : ''}? Files stay on disk.`)) return;
  snapshot();
  B.cards = B.cards.filter(c => !sel.has(c.id));
  B.edges = B.edges.filter(e => !sel.has(e.from) && !sel.has(e.to));
  for (const id of sel) { mounted.get(id)?.remove(); mounted.delete(id); }
  sel.clear(); render(); markDirty(); refreshScenes();
}
function bringFront(c) { c.z = ++maxZ; }

// ---------------------------------------------------------------- upload
async function upload(file) {
  const fd = new FormData(); fd.append('file', file);
  return api('/api/board-upload', {method: 'POST', body: fd});
}
async function dropFiles(files, at) {
  const list = [...files]; if (!list.length) return;
  const cols = Math.ceil(Math.sqrt(list.length));
  const pending = list.map((f, i) => {
    const kind = f.type.startsWith('image/') ? 'image' : f.type.startsWith('video/') ? 'video' : f.type.startsWith('audio/') ? 'audio'
      : f.type === 'application/pdf' ? 'pdf' : guessKind(f.name);
    const c = addCard(kind, {x: at.x + (i % cols) * 320, y: at.y + Math.floor(i / cols) * 260}, {title: f.name});
    return [f, c];
  });
  for (const [f, c] of pending) {
    try {
      const r = await upload(f);
      Object.assign(c.data, {asset: r.asset, name: r.name, width: r.width, height: r.height, duration: r.duration, status: r.status});
      if (r.kind !== c.type && r.kind !== 'file') { c.type = r.kind; }
      if (r.width && r.height) { c.data.aspect = r.width / r.height; c.h = Math.round(HEAD + c.w / c.data.aspect); }
      touch(c); refreshCard(c.id); place(c, mounted.get(c.id));
    } catch (e) { c.data.error = String(e); refreshCard(c.id); }
  }
  schedulePoll();
}
function guessKind(name) {
  const x = name.toLowerCase().split('.').pop();
  if (/^(png|jpe?g|webp|gif|tiff?|bmp|exr|heic|dpx)$/.test(x)) return 'image';
  if (/^(mp4|mov|webm|mkv|avi|m4v|mxf)$/.test(x)) return 'video';
  if (/^(mp3|wav|aac|m4a|flac|ogg|aiff?)$/.test(x)) return 'audio';
  if (x === 'pdf') return 'pdf';
  return 'file';
}
function applyAssetReady(a) {
  let hit = false;
  for (const c of B.cards) {
    if (c.data?.asset === a.asset) {
      Object.assign(c.data, {thumb: a.thumb || c.data.thumb, proxy: a.proxy || c.data.proxy, wave: a.wave || c.data.wave, status: 'ready'});
      refreshCard(c.id); hit = true;
    } else if (c.type === 'subject' && (c.data.refs || []).includes(a.asset)) refreshCard(c.id);
  }
  if (hit) markDirty();
}
// fallback when SSE is down: poll status of pending assets
function schedulePoll() {
  clearTimeout(pendingPoll);
  const pend = B.cards.filter(c => isMedia.test(c.type) && c.data?.asset && c.data.status !== 'ready');
  if (!pend.length) return;
  pendingPoll = setTimeout(async () => {
    for (const c of pend) {
      try { const s = await api(`/api/assets/${encodeURIComponent(c.data.name || c.data.asset.split('/').pop())}/status`);
        if (s.status === 'ready') applyAssetReady(s); } catch {}
    }
    schedulePoll();
  }, 3000);
}

// ---------------------------------------------------------------- SSE
function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource(`/api/projects/${PID}/board/events`);
  const dot = $('#sse-dot');
  sse.onopen = () => dot.className = 'dot ok';
  sse.onerror = () => dot.className = 'dot err';
  sse.addEventListener('asset.ready', e => applyAssetReady(JSON.parse(e.data)));
  sse.addEventListener('take.ready', e => {
    const {card, edge, scene} = JSON.parse(e.data);
    if (!byId(card.id)) B.cards.push(card);
    if (!B.edges.some(x => x.from === edge.from && x.to === edge.to)) B.edges.push(edge);
    const s = byId(scene); if (s) { s.data.takes = (s.data.takes || 0) + 1; refreshCard(scene); }
    maxZ = Math.max(maxZ, card.z || 0);
    render();
  });
  sse.addEventListener('card.updated', e => {
    for (const o of JSON.parse(e.data).ops) {
      if (o.edge) {
        if (o.op === 'add') B.edges.push(o.edge);
        else B.edges = B.edges.filter(x => !(x.from === o.edge.from && x.to === o.edge.to));
        continue;
      }
      const cur = byId(o.card.id);
      if (o.op === 'delete') { B.cards = B.cards.filter(c => c.id !== o.card.id); B.edges = B.edges.filter(x => x.from !== o.card.id && x.to !== o.card.id); mounted.get(o.card.id)?.remove(); mounted.delete(o.card.id); }
      else if (cur) { Object.assign(cur, o.card); refreshCard(cur.id); }
      else B.cards.push(o.card);
    }
    render();
  });
}

// ---------------------------------------------------------------- gestures
let space = false, drag = null, dragMoved = false;
const interactive = t => t.closest('input,textarea,select,button,a,audio,.refs,.tag');

canvas.addEventListener('pointerdown', e => {
  canvas.focus();
  const p = canvasPoint(e), w = toWorld(p.x, p.y);
  const cardEl = e.target.closest('.card');
  dragMoved = false;
  if (e.button === 1 || (e.button === 0 && space) || (e.button === 0 && !cardEl && e.altKey)) {
    drag = {kind: 'pan', sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y};
    canvas.setPointerCapture(e.pointerId); return;
  }
  if (e.button !== 0) return;
  if (cardEl) {
    const c = byId(cardEl.dataset.id); if (!c) return;
    if (e.target.classList.contains('grip')) {
      snapshot(); drag = {kind: 'resize', c, sx: e.clientX, sy: e.clientY, w: c.w, h: c.h};
      canvas.setPointerCapture(e.pointerId); return;
    }
    if (!sel.has(c.id)) { if (!e.shiftKey) sel.clear(); sel.add(c.id); }
    else if (e.shiftKey) sel.delete(c.id);
    bringFront(c);
    for (const id of sel) { const m = mounted.get(id); if (m) place(byId(id), m); }
    place(c, cardEl);
    if (interactive(e.target)) return;
    const members = new Set(sel);
    for (const id of sel) { const g = byId(id); if (g?.type === 'group') for (const x of B.cards) if (x.id !== g.id && inside(x, g)) members.add(x.id); }
    drag = {kind: 'move', sx: e.clientX, sy: e.clientY, start: [...members].map(id => { const x = byId(id); return [x, x.x, x.y]; }), snap: false};
    canvas.setPointerCapture(e.pointerId);
    e.preventDefault();
    return;
  }
  if (!e.shiftKey) { sel.clear(); render(); }
  drag = {kind: 'lasso', sx: p.x, sy: p.y, keep: e.shiftKey};
  canvas.setPointerCapture(e.pointerId);
});
const inside = (a, g) => a.x >= g.x && a.y >= g.y && a.x + a.w <= g.x + g.w && a.y + a.h <= g.y + g.h;

canvas.addEventListener('pointermove', e => {
  if (!drag) return;
  const dx = (e.clientX - drag.sx), dy = (e.clientY - drag.sy);
  if (Math.abs(dx) + Math.abs(dy) > 3) dragMoved = true;
  if (drag.kind === 'pan') { view.x = drag.vx + dx; view.y = drag.vy + dy; applyView(); return; }
  if (drag.kind === 'move') {
    if (!dragMoved) return;
    if (!drag.snap) { snapshot(); drag.snap = true; }
    for (const [c, x0, y0] of drag.start) { c.x = Math.round(x0 + dx / view.s); c.y = Math.round(y0 + dy / view.s); const m = mounted.get(c.id); if (m) place(c, m); }
    drawEdges(); return;
  }
  if (drag.kind === 'resize') {
    const c = drag.c; c.w = Math.max(120, Math.round(drag.w + dx / view.s));
    c.h = c.data?.aspect && isMedia.test(c.type) ? Math.round(HEAD + c.w / c.data.aspect) : Math.max(60, Math.round(drag.h + dy / view.s));
    const m = mounted.get(c.id); if (m) place(c, m); drawEdges(); return;
  }
  if (drag.kind === 'lasso') {
    const p = canvasPoint(e), L = $('#lasso');
    const x = Math.min(p.x, drag.sx), y = Math.min(p.y, drag.sy), w = Math.abs(p.x - drag.sx), h = Math.abs(p.y - drag.sy);
    L.hidden = false; Object.assign(L.style, {left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px'});
    const a = toWorld(x, y), b = toWorld(x + w, y + h);
    if (!drag.keep) sel.clear();
    for (const c of B.cards) if (c.x < b.x && c.x + c.w > a.x && c.y < b.y && c.y + c.h > a.y) sel.add(c.id);
    for (const [id, m] of mounted) m.classList.toggle('selected', sel.has(id));
  }
});
canvas.addEventListener('pointerup', e => {
  if (!drag) return;
  if (drag.kind === 'move' && dragMoved) { for (const [c] of drag.start) c.updated = Date.now() / 1000; markDirty(); }
  if (drag.kind === 'resize') { drag.c.updated = Date.now() / 1000; markDirty(); }
  if (drag.kind === 'lasso') $('#lasso').hidden = true;
  drag = null;
  setTimeout(() => dragMoved = false, 0);
});
canvas.addEventListener('dblclick', e => {
  if (e.target.closest('.card')) return;
  const p = canvasPoint(e); addCard('text', toWorld(p.x, p.y));
});
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const p = canvasPoint(e);
  if (e.ctrlKey || e.metaKey) zoomAt(p.x, p.y, Math.exp(-e.deltaY * 0.01));
  else { view.x -= e.deltaX; view.y -= e.deltaY; applyView(); }
}, {passive: false});

const inField = () => ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
window.addEventListener('keydown', e => {
  if (e.key === 'Escape') { if (!$('#lightbox').hidden) { $('#lightbox').hidden = true; replace($('#lightbox')); } else { document.activeElement?.blur(); sel.clear(); render(); } return; }
  if (inField()) return;
  if (e.code === 'Space') { space = true; canvas.classList.add('panning'); e.preventDefault(); return; }
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && e.key.toLowerCase() === 'a') { e.preventDefault(); for (const c of B.cards) sel.add(c.id); render(); return; }
  if (mod) return;
  const k = e.key.toLowerCase();
  if (k === 'f') fit();
  else if (k === 't') addCard('text', center());
  else if (k === 'b') addCard('group', center());
  else if (k === 's') addCard('subject', center());
  else if (k === 'n') addCard('scene', center());
  else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteSelected(); }
});
window.addEventListener('keyup', e => { if (e.code === 'Space') { space = false; canvas.classList.remove('panning'); } });

// drag & drop from the Finder or another window
canvas.addEventListener('dragover', e => { e.preventDefault(); canvas.classList.add('dropping'); });
canvas.addEventListener('dragleave', e => { if (e.target === canvas) canvas.classList.remove('dropping'); });
canvas.addEventListener('drop', e => {
  e.preventDefault(); canvas.classList.remove('dropping');
  const p = canvasPoint(e), w = toWorld(p.x, p.y);
  if (e.dataTransfer.files.length) return dropFiles(e.dataTransfer.files, w);
  const url = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
  if (url && /^https?:\/\//i.test(url.trim())) { const c = addCard('link', w); setLink(c, url.trim()); refreshCard(c.id); }
  else if (url) addCard('text', w, {data: {text: url}});
});
window.addEventListener('paste', e => {
  if (inField()) return;
  const files = [...e.clipboardData.files];
  if (files.length) return dropFiles(files, center());
  const t = e.clipboardData.getData('text/plain').trim(); if (!t) return;
  if (/^https?:\/\/\S+$/i.test(t)) { const c = addCard('link', center()); setLink(c, t); refreshCard(c.id); }
  else addCard('text', center(), {data: {text: t}});
});
window.addEventListener('resize', render);

// ---------------------------------------------------------------- header
function bindUI() {
  for (const b of document.querySelectorAll('[data-add]')) b.onclick = () => { if (PID) addCard(b.dataset.add, center()); };
  $('#file-input').onchange = e => { if (PID) dropFiles(e.target.files, center()); e.target.value = ''; };
  $('#project-select').onchange = async e => { await flushSave(); PID = e.target.value; await loadBoard(); };
  $('#new-project').onclick = async () => {
    const name = prompt('Project name'); if (!name) return;
    const fd = new FormData(); fd.append('name', name);
    const p = await api('/api/projects', {method: 'POST', body: fd});
    STATE.projects[p.id] = p; PID = p.id; renderProjects(); await loadBoard();
  };
}

boot();
