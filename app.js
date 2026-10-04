(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const IS_TOUCH = matchMedia('(hover: none)').matches;
const SMALL_Q = '(max-width: 720px), (max-height: 500px) and (pointer: coarse)';     // keep in sync with style.css
const small = () => matchMedia(SMALL_Q).matches;

// ---------------------------------------------------------------- palettes
// Sequential, monotone in lightness (dark -> light = cheap -> expensive) for a dark base
// map, with the darkest step still about 4:1 against the land color. Generated in OKLCH;
// "cvd" keeps to the blue-yellow axis that survives protanopia and deuteranopia.
// 12 steps for home values, 10 for rents.
const PALETTES = {
  heat: {
    12: ['#1a79c2', '#5274da', '#7f6ce8', '#a964e6', '#cf5ed5', '#f05aba', '#fe729b', '#fe918d', '#ffab89', '#ffc492', '#fedda3', '#fff4b1'],
    10: ['#1a79c2', '#5c72de', '#9267ea', '#c260dc', '#ec5bbd', '#fe7699', '#fd9b8c', '#feb98e', '#fed79f', '#fff4b1'],
  },
  cvd: {
    12: ['#3e6dc8', '#1b7ec9', '#118cbf', '#0899b8', '#06a8b2', '#10b7a8', '#18c794', '#60d37d', '#96da5f', '#c7e03e', '#e9e83c', '#fff19b'],
    10: ['#3e6dc8', '#0b81c9', '#1192bb', '#0ea2b4', '#07b6a9', '#25c992', '#73d673', '#b3dd4a', '#e2e63c', '#fff19b'],
  },
};
const NO_CASH = '#7b808a';

// ---------------------------------------------------------------- state
const S = {
  mode: 'value',            // 'value' | 'rent'
  view: 'dots',             // 'dots' | 'areas'
  pal: 'heat',
  labels: true,
  hidden: { value: new Set(), rent: new Set() },   // hidden class indices
  sel: null,                // { level: 'c'|'t'|'b'|'s'|'us', id, origin: {level, id} }
};
let M = null;               // manifest
let SUM = null;             // summary.json (loads in the background)
let US_SHAPE = null;        // coarse US outline, for dimming foreign place names
let map = null;
let hitReady = false;       // area hit-test tiles are deferred until the dots have drawn
const FADE = 0.6;           // zoom range over which dot tiers cross-fade
const sumP = fetch('data/summary.json').then(r => { if (!r.ok) throw new Error('summary ' + r.status); return r.json(); })
  .then(d => { SUM = d; return d; });
sumP.catch(() => {});

// ---------------------------------------------------------------- tile archives
// Dots and area outlines are PMTiles archives, some split into column shards to keep every
// file small enough for any static host. A custom protocol routes each tile to its shard.
const archives = new Map();
function archive(file) {
  if (!archives.has(file)) archives.set(file, new pmtiles.PMTiles(new URL('data/' + file, location.href).href));
  return archives.get(file);
}
function shardFor(shards, maxzoom, z, x) {
  const col = z > maxzoom ? x >> (z - maxzoom) : x << Math.max(0, maxzoom - z);
  return shards.find(s => col >= s.x0 && col <= s.x1) || shards[0];
}
// Buffers are transferred to the worker (and detached), so every response needs a fresh one.
const empty = () => ({ data: new ArrayBuffer(0) });
maplibregl.addProtocol('hp', async (params, abort) => {
  // hp://dots/<tier>/<value|rent>/z/x/y   or   hp://areas/<set>/z/x/y
  const m = params.url.match(/^hp:\/\/(dots\/\w+\/\w+|areas\/\w+)\/(\d+)\/(\d+)\/(\d+)/);
  if (!m) return empty();
  const [, path, z, x, y] = m;
  const p = path.split('/');
  let shards, maxzoom, dir;
  if (p[0] === 'dots') {
    const t = M.tiers.find(t => t.name === p[1]);
    if (!t || !t.files[p[2]]) return empty();
    shards = t.files[p[2]]; maxzoom = t.maxzoom; dir = 'dots/';
  } else {
    const a = M.areas[p[1]];
    if (!a) return empty();
    shards = a.shards; maxzoom = a.maxzoom; dir = 'areas/';
  }
  const r = await archive(dir + shardFor(shards, maxzoom, +z, +x).file).getZxy(+z, +x, +y, abort && abort.signal);
  return r && r.data ? { data: r.data } : empty();
});

// ---------------------------------------------------------------- formatting
// ACS publishes some medians as codes: top codes (value 2,000,001 = "$2M+", rent 3,501,
// income 250,001, rent burden 51 = "50%+") and bottom codes (value 9,999 = "under $10K",
// rent 99, income 2,499, rent burden 9 = "10% or less").
const TOP = { value: 2000001, rent: 3501, income: 250001 };
const BOTTOM = { value: 9999, rent: 99, income: 2499 };
const isTop = (v, kind) => v != null && v >= TOP[kind];
const isBottom = (v, kind) => v != null && v <= BOTTOM[kind];
function money(v, kind) {
  if (v == null || !isFinite(v)) return '—';
  if (kind === 'rent') {
    if (isTop(v, kind)) return '$3,500+';
    if (isBottom(v, kind)) return 'Under $100';
    return '$' + Math.round(v).toLocaleString('en-US');
  }
  if (isTop(v, kind)) return kind === 'income' ? '$250K+' : '$2M+';
  if (isBottom(v, kind)) return kind === 'income' ? 'Under $2,500' : 'Under $10K';
  if (v >= 999500) return '$' + (v / 1e6).toFixed(v >= 1e7 ? 0 : 2).replace(/\.?0+$/, '') + 'M';
  if (v >= 1e4) return '$' + Math.round(v / 1e3) + 'K';
  return '$' + Math.round(v).toLocaleString('en-US');
}
// plain dollar amounts (margins of error): no top/bottom-code wording
function dollars(v) {
  if (v == null || !isFinite(v)) return '';
  if (v >= 999500) return '$' + (v / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (v >= 1e4) return '$' + Math.round(v / 1e3) + 'K';
  return '$' + Math.round(v).toLocaleString('en-US');
}
function burden(b) {
  if (b == null) return '—';
  if (b >= 50.05) return '50%+';
  if (b <= 9.95) return '10% or less';
  return b.toFixed(1) + '%';
}
const n0 = v => v == null ? '—' : Math.round(v).toLocaleString('en-US');
const pct = (a, b) => b > 0 ? Math.round(100 * a / b) + '%' : '—';
function tractName(id) {
  const t = id.slice(5, 11);
  const main = String(parseInt(t.slice(0, 4), 10)), suf = t.slice(4);
  return 'Tract ' + main + (suf !== '00' ? '.' + suf : '');
}
const stateRow = id => SUM && SUM.state[id.slice(0, 2)];
function stateAbbr(id) { const s = stateRow(id); return s ? s[0] : ''; }
function stateName(id) { const s = stateRow(id); return s ? s[1] : ''; }
function countyName(id) {
  const c = SUM && SUM.county[id.slice(0, 5)];
  return c ? c[0] + ', ' + stateAbbr(id) : '';
}
function areaTitle(level, id) {
  if (level === 'us') return 'United States';
  if (level === 's') return stateName(id) || 'State';
  if (level === 'c') return countyName(id) || 'County';
  if (level === 't') return tractName(id);
  return 'Block group ' + id.slice(11);
}
function areaWhere(level, id) {
  if (level === 'b') return tractName(id) + (countyName(id) ? ' · ' + countyName(id) : '');
  if (level === 't') return countyName(id);
  if (level === 'c') return stateName(id);
  if (level === 's') return 'State';
  return '';
}

// ---------------------------------------------------------------- classes & colors
function classes(mode = S.mode) { return M[mode].classes; }      // [label, lo, hi]
// legend rows shown in the current view ("No cash rent" has no median color)
function shownClasses(mode = S.mode) {
  return classes(mode).map((c, i) => i).filter(i => !(S.view === 'areas' && classes(mode)[i][0] === 'No cash rent'));
}
function colors(mode = S.mode) {
  const cls = classes(mode);
  const priced = cls.filter(c => c[0] !== 'No cash rent').length;
  const ramp = PALETTES[S.pal][priced];
  return cls.map((c, i) => c[0] === 'No cash rent' ? NO_CASH : ramp[i]);
}
// MapLibre expression: ACS bin -> class color
function dotColorExpr(mode) {
  const cls = classes(mode), col = colors(mode), expr = ['match', ['get', 'b']];
  cls.forEach(([, lo, hi], i) => { const bins = []; for (let b = lo; b <= hi; b++) bins.push(b); expr.push(bins, col[i]); });
  expr.push('#000');
  return expr;
}
function visibleBins(mode) {
  const out = [];
  classes(mode).forEach(([, lo, hi], i) => { if (!S.hidden[mode].has(i)) for (let b = lo; b <= hi; b++) out.push(b); });
  return out;
}
// lower dollar edge of each class, for coloring medians
function classEdges(mode) { return classes(mode).filter(c => c[0] !== 'No cash rent').map(c => M[mode].edges[c[1]]); }
function classOf(mode, v) {
  if (v == null || !isFinite(v)) return -1;
  const e = classEdges(mode); let k = 0;
  for (let i = 0; i < e.length; i++) if (v >= e[i]) k = i;
  return k;
}
function areaColorExpr(mode) {
  const field = mode === 'value' ? 'v' : 'g', e = classEdges(mode), col = colors(mode);
  const step = ['step', ['to-number', ['get', field]], col[0]];
  for (let i = 1; i < e.length; i++) step.push(e[i], col[i]);
  return ['case', ['has', field], step, 'rgba(0,0,0,0)'];
}
function areaFilter(mode) {
  const field = mode === 'value' ? 'v' : 'g';
  if (!S.hidden[mode].size) return ['has', 'id'];
  const e = classEdges(mode), conds = ['any'];
  e.forEach((lo, i) => {
    if (S.hidden[mode].has(i)) return;
    const hi = e[i + 1];
    conds.push(hi == null ? ['>=', ['to-number', ['get', field]], lo]
      : ['all', ['>=', ['to-number', ['get', field]], lo], ['<', ['to-number', ['get', field]], hi]]);
  });
  return ['all', ['has', field], conds];
}

// ---------------------------------------------------------------- base map style (OpenFreeMap / OpenMapTiles)
const OFM = 'https://tiles.openfreemap.org';
const P = {
  land: '#111318', water: '#0a0d13', park: '#121811', urban: '#14161b',
  road: '#1f232b', roadHi: '#262a33', motor: '#2a2e38', bnd: '#3b404a', bnd2: '#555b66', building: '#181b21',
  label: '#c4c9d2', label2: '#8b919c', halo: 'rgba(10,12,16,0.92)', waterLabel: '#4f6582',
};
const W = (pairs) => ['interpolate', ['exponential', 1.5], ['zoom'], ...pairs.flat()];
const NAME = ['coalesce', ['get', 'name:en'], ['get', 'name_en'], ['get', 'name']];
function baseLayers() {
  const B = { source: 'base' };
  const cls = (...c) => ['in', ['get', 'class'], ['literal', c]];
  const L = [];
  L.push({ id: 'bg', type: 'background', paint: { 'background-color': P.land } });
  L.push({ ...B, id: 'park', type: 'fill', 'source-layer': 'park', minzoom: 8, paint: { 'fill-color': P.park, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 8, 0, 11, 1] } });
  L.push({ ...B, id: 'landuse', type: 'fill', 'source-layer': 'landuse', minzoom: 10, filter: cls('residential', 'commercial', 'industrial', 'retail'), paint: { 'fill-color': P.urban } });
  L.push({ ...B, id: 'water', type: 'fill', 'source-layer': 'water', filter: ['!=', ['get', 'brunnel'], 'tunnel'], paint: { 'fill-color': P.water } });
  L.push({ ...B, id: 'waterway', type: 'line', 'source-layer': 'waterway', minzoom: 8, filter: cls('river', 'canal'), paint: { 'line-color': P.water, 'line-width': W([[8, 0.6], [14, 3], [18, 10]]) } });
  L.push({ ...B, id: 'building', type: 'fill', 'source-layer': 'building', minzoom: 14, paint: { 'fill-color': P.building, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 14, 0, 15, 0.9] } });
  const ROADS = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'minor', 'service'];
  const roadW = (stops) => ['interpolate', ['exponential', 1.5], ['zoom'], ...stops.flatMap(([z, m, t, p, s, te, mi, sv]) => [z, ['match', ['get', 'class'], 'motorway', m, 'trunk', t, 'primary', p, 'secondary', s, 'tertiary', te, 'minor', mi, sv]])];
  L.push({ ...B, id: 'road', type: 'line', 'source-layer': 'transportation', minzoom: 5,
    filter: ['all', ['==', ['geometry-type'], 'LineString'], cls(...ROADS), ['!=', ['get', 'brunnel'], 'tunnel'],
      ['any', ['>=', ['zoom'], 12], cls('motorway', 'trunk'), ['all', ['>=', ['zoom'], 8], cls('primary')], ['all', ['>=', ['zoom'], 10], cls('secondary', 'tertiary')]]],
    layout: { 'line-join': 'round', 'line-cap': 'round', visibility: S.labels ? 'visible' : 'none' },
    paint: { 'line-color': ['match', ['get', 'class'], ['motorway', 'trunk'], P.motor, ['primary', 'secondary'], P.roadHi, P.road],
      'line-width': roadW([[5, 0.5, 0.4, 0.3, 0.2, 0.2, 0.2, 0.1], [8, 0.9, 0.8, 0.6, 0.4, 0.3, 0.2, 0.1], [12, 2, 1.8, 1.5, 1.2, 1, 0.7, 0.4], [15, 6, 5.5, 5, 4, 3.4, 2.6, 1.4], [18, 20, 18, 16, 14, 12, 10, 5]]) } });
  L.push({ ...B, id: 'bnd4', type: 'line', 'source-layer': 'boundary', minzoom: 2, filter: ['all', ['==', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd, 'line-width': W([[3, 0.5], [8, 1], [14, 1.6]]) } });
  L.push({ ...B, id: 'bnd2', type: 'line', 'source-layer': 'boundary', filter: ['all', ['==', ['get', 'admin_level'], 2], ['!=', ['get', 'maritime'], 1], ['!=', ['get', 'disputed'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd2, 'line-width': W([[2, 0.7], [8, 1.4]]) } });
  return L;
}
// Place names inside the US draw at full strength above the mask; names elsewhere are dimmed.
// The split needs data/us.json, which loads in the background (see splitLabels).
function labelLayers() {
  const B = { source: 'base' };
  const vis = S.labels ? 'visible' : 'none';
  const font = ['Noto Sans Regular'], bold = ['Noto Sans Bold'];
  const halo = { 'text-halo-color': P.halo, 'text-halo-width': 1.4, 'text-halo-blur': 0.4 };
  const defs = [
    { id: 'lbl-water', 'source-layer': 'water_name', minzoom: 3, filter: ['==', ['geometry-type'], 'Point'],
      layout: { 'text-field': NAME, 'text-font': ['Noto Sans Italic'], 'text-size': W([[3, 10], [10, 13]]), 'text-max-width': 6, 'text-letter-spacing': 0.08 },
      paint: { 'text-color': P.waterLabel, ...halo } },
    { id: 'lbl-road', 'source-layer': 'transportation_name', minzoom: 13, split: false,
      layout: { 'symbol-placement': 'line', 'text-field': NAME, 'text-font': font, 'text-size': W([[13, 10], [17, 13]]), 'text-max-angle': 30 },
      paint: { 'text-color': P.label2, ...halo } },
    { id: 'lbl-place-minor', 'source-layer': 'place', minzoom: 10, filter: ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter', 'hamlet', 'village']]],
      layout: { 'text-field': NAME, 'text-font': font, 'text-size': W([[10, 10.5], [15, 13]]), 'text-max-width': 7, 'text-transform': ['case', ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter']]], 'uppercase', 'none'], 'text-letter-spacing': ['case', ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter']]], 0.08, 0] },
      paint: { 'text-color': P.label2, ...halo } },
    { id: 'lbl-town', 'source-layer': 'place', minzoom: 7, filter: ['==', ['get', 'class'], 'town'],
      layout: { 'text-field': NAME, 'text-font': font, 'text-size': W([[7, 10.5], [12, 14]]), 'text-max-width': 7 },
      paint: { 'text-color': P.label, ...halo } },
    { id: 'lbl-city', 'source-layer': 'place', minzoom: 3, filter: ['==', ['get', 'class'], 'city'],
      layout: { 'text-field': NAME, 'text-font': ['step', ['zoom'], ['literal', font], 6, ['literal', bold]], 'text-size': ['interpolate', ['linear'], ['zoom'], 3, ['case', ['<=', ['coalesce', ['get', 'rank'], 9], 3], 12, 10.5], 10, ['case', ['<=', ['coalesce', ['get', 'rank'], 9], 3], 18, 15]],
        'text-max-width': 7, 'symbol-sort-key': ['coalesce', ['get', 'rank'], 99] },
      paint: { 'text-color': '#e4e7ec', ...halo, 'text-halo-width': 1.6 } },
    { id: 'lbl-state', 'source-layer': 'place', minzoom: 3.2, maxzoom: 7, filter: ['==', ['get', 'class'], 'state'],
      layout: { 'text-field': NAME, 'text-font': bold, 'text-size': W([[3, 9.5], [6, 13]]), 'text-transform': 'uppercase', 'text-letter-spacing': 0.14, 'text-max-width': 8 },
      paint: { 'text-color': '#7d8390', ...halo } },
  ];
  const out = [];
  for (const d of defs) {
    const { split, ...rest } = d;
    const base = { ...B, type: 'symbol', ...rest, layout: { ...rest.layout, visibility: vis } };
    out.push(base);
    if (!US_SHAPE || split === false) continue;
    const inUS = ['within', US_SHAPE];
    base.filter = d.filter ? ['all', d.filter, inUS] : inUS;
    out.push({ ...base, id: d.id + '-far', filter: d.filter ? ['all', d.filter, ['!', inUS]] : ['!', inUS], paint: { ...d.paint, 'text-opacity': 0.4 }, _below: true });
  }
  return out;
}
function splitLabels() {
  for (const l of labelLayers()) {
    if (l._below) { const { _below, ...far } = l; if (!map.getLayer(far.id)) map.addLayer(far, 'mask'); }
    else if (map.getLayer(l.id) && l.filter) map.setFilter(l.id, l.filter);
  }
}

// ---------------------------------------------------------------- data layers
// Base dot radius by zoom; each tier grows its dots by up to 30% across its range so that
// coverage stays even as the next tier (2.5x more dots) takes over.
const R_STOPS = [[0, 0.55], [3, 0.75], [5, 0.95], [7, 1.15], [9, 1.35], [11, 1.6], [12, 1.8], [14, 2.6], [16, 4.6], [18, 9]];
function baseRadius(z) {
  if (z <= R_STOPS[0][0]) return R_STOPS[0][1];
  for (let i = 1; i < R_STOPS.length; i++) {
    const [z1, r1] = R_STOPS[i], [z0, r0] = R_STOPS[i - 1];
    if (z <= z1) return r0 * Math.pow(r1 / r0, (z - z0) / (z1 - z0));
  }
  return R_STOPS[R_STOPS.length - 1][1];
}
function tierRadius(t, last) {
  const [s, e] = t.show, zs = new Set(R_STOPS.map(r => r[0]));
  if (!last) for (let z = s; z <= e + FADE + 1e-9; z += 0.5) zs.add(+z.toFixed(2));
  const stops = [];
  [...zs].sort((a, b) => a - b).forEach(z => {
    const grow = last ? 1 : 1 + 0.3 * Math.min(1, Math.max(0, (z - s) / (e - s)));
    stops.push(z, +(baseRadius(z) * grow).toFixed(3));
  });
  return ['interpolate', ['linear'], ['zoom'], ...stops];
}
function dotLayers() {
  const L = [];
  const last = M.tiers.length - 1;
  M.tiers.forEach((t, i) => {
    const [s, e] = t.show;
    const stops = i === 0 ? [0, 1] : [s, 0, s + FADE, 1];
    if (i !== last) stops.push(e, 1, e + FADE, 0);
    ['value', 'rent'].forEach(mode => {
      L.push({
        id: `dot-${t.name}-${mode}`, type: 'circle', source: `d-${t.name}-${mode}`, 'source-layer': mode === 'value' ? 'v' : 'r',
        minzoom: i === 0 ? 0 : s, maxzoom: i === last ? 24 : e + FADE,
        filter: ['in', ['get', 'b'], ['literal', visibleBins(mode)]],
        layout: { visibility: S.view === 'dots' && S.mode === mode ? 'visible' : 'none' },
        paint: {
          'circle-color': dotColorExpr(mode),
          'circle-radius': tierRadius(t, i === last),
          'circle-opacity': ['interpolate', ['linear'], ['zoom'], ...stops.map((v, k) => k % 2 ? v * 0.92 : v)],
          'circle-pitch-alignment': 'map',
        },
      });
    });
  });
  return L;
}
const LEVELS = [   // area outline layers: county below z8, tract 8-10, block group 10+
  { key: 'c', set: 'county', minzoom: 0, maxzoom: 8 },
  { key: 't', set: 'tract', minzoom: 8, maxzoom: 10 },
  { key: 'b', set: 'bg', minzoom: 10, maxzoom: 24 },
];
const levelOf = k => LEVELS.find(l => l.key === k);
const hitVisible = () => S.view === 'areas' || hitReady;
function areaFillLayers() {
  return LEVELS.map(l => ({
    id: `af-${l.key}`, type: 'fill', source: `a-${l.key}`, 'source-layer': M.areas[l.set].layer, minzoom: l.minzoom, maxzoom: l.maxzoom,
    filter: S.view === 'areas' ? areaFilter(S.mode) : ['has', 'id'],
    layout: { visibility: hitVisible() ? 'visible' : 'none' },
    paint: { 'fill-color': S.view === 'areas' ? areaColorExpr(S.mode) : '#000', 'fill-opacity': S.view === 'areas' ? 0.88 : 0, 'fill-antialias': false },
  }));
}
function areaLineLayers() {
  const L = [];
  for (const l of LEVELS) {
    const base = { source: `a-${l.key}`, 'source-layer': M.areas[l.set].layer, minzoom: l.minzoom, maxzoom: l.maxzoom, layout: { 'line-join': 'round' } };
    L.push({ ...base, id: `ao-${l.key}`, type: 'line', layout: { ...base.layout, visibility: S.view === 'areas' ? 'visible' : 'none' },
      paint: { 'line-color': 'rgba(10,12,16,0.55)', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.2, 10, 0.6, 14, 1] } });
    const hl = { ...base.layout, visibility: hitVisible() ? 'visible' : 'none' };
    L.push({ ...base, id: `ah-${l.key}`, type: 'line', filter: ['==', ['get', 'id'], ''], layout: hl,
      paint: { 'line-color': 'rgba(255,255,255,0.85)', 'line-width': 1.3 } });
    L.push({ ...base, id: `as-case-${l.key}`, type: 'line', filter: ['==', ['get', 'id'], ''], layout: hl,
      paint: { 'line-color': 'rgba(0,0,0,0.75)', 'line-width': 4.5 } });
    L.push({ ...base, id: `as-${l.key}`, type: 'line', filter: ['==', ['get', 'id'], ''], layout: hl,
      paint: { 'line-color': '#ffffff', 'line-width': 2 } });
  }
  return L;
}
function buildStyle() {
  const sources = {
    base: { type: 'vector', url: OFM + '/planet' },
    mask: { type: 'geojson', data: 'data/mask.json' },
  };
  M.tiers.forEach(t => ['value', 'rent'].forEach(mode => {
    sources[`d-${t.name}-${mode}`] = { type: 'vector', tiles: [`hp://dots/${t.name}/${mode}/{z}/{x}/{y}`], minzoom: t.minzoom, maxzoom: t.maxzoom };
  }));
  LEVELS.forEach(l => { const a = M.areas[l.set]; sources[`a-${l.key}`] = { type: 'vector', tiles: [`hp://areas/${l.set}/{z}/{x}/{y}`], minzoom: a.minzoom, maxzoom: a.maxzoom }; });
  const labels = labelLayers().map(l => { const { _below, ...rest } = l; return { l: rest, below: !!_below }; });   // unsplit until us.json loads
  // The mask fills foreign land and open sea with the water color: it darkens Canada and Mexico
  // and matches US waters exactly, so it leaves no halo around coasts and islands.
  const mask = { id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': P.water, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.7, 9, 0.6, 13, 0.35] } };
  return {
    version: 8,
    glyphs: OFM + '/fonts/{fontstack}/{range}.pbf',
    sources,
    layers: [...baseLayers(), ...areaFillLayers(), ...dotLayers(), ...areaLineLayers(),
      ...labels.filter(x => x.below).map(x => x.l), mask, ...labels.filter(x => !x.below).map(x => x.l)],
  };
}

// ---------------------------------------------------------------- apply state to the map
function applyMode(opts = {}) {
  for (const t of M.tiers) for (const mode of ['value', 'rent']) {
    const id = `dot-${t.name}-${mode}`;
    map.setLayoutProperty(id, 'visibility', S.view === 'dots' && S.mode === mode ? 'visible' : 'none');
    map.setPaintProperty(id, 'circle-color', dotColorExpr(mode));
    map.setFilter(id, ['in', ['get', 'b'], ['literal', visibleBins(mode)]]);
  }
  setHitVisibility();
  for (const l of LEVELS) {
    map.setPaintProperty(`af-${l.key}`, 'fill-color', S.view === 'areas' ? areaColorExpr(S.mode) : '#000');
    map.setPaintProperty(`af-${l.key}`, 'fill-opacity', S.view === 'areas' ? 0.88 : 0);
    map.setFilter(`af-${l.key}`, S.view === 'areas' ? areaFilter(S.mode) : ['has', 'id']);
    map.setLayoutProperty(`ao-${l.key}`, 'visibility', S.view === 'areas' ? 'visible' : 'none');
  }
  renderLegend();
  syncSegs();
  updateRatio();
  if (S.sel && opts.colors) renderDetail({ keepScroll: true });   // only the bar colors depend on these settings
  writeHash();
}
function applyLabels() {
  for (const l of labelLayers()) if (map.getLayer(l.id)) map.setLayoutProperty(l.id, 'visibility', S.labels ? 'visible' : 'none');
  map.setLayoutProperty('road', 'visibility', S.labels ? 'visible' : 'none');
}
// segmented radio groups: checked state + roving tabindex
function syncSegs() {
  const sets = [['#modeSeg', 'mode', S.mode], ['#viewSeg', 'view', S.view], ['#palSeg', 'pal', S.pal]];
  for (const [sel, key, val] of sets) $$(sel + ' button').forEach(b => {
    const on = b.dataset[key] === val;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  });
}

// ---------------------------------------------------------------- legend
function renderLegend() {
  const cls = classes(), col = colors();
  const us = M[S.mode].us_classes || [];
  const tot = us.reduce((a, b) => a + b, 0);
  const max = us.length ? Math.max(...us) : 1;
  const focusI = document.activeElement && document.activeElement.closest && document.activeElement.closest('.lrow') ? document.activeElement.closest('.lrow').dataset.i : null;
  const label = S.view === 'areas' ? (S.mode === 'value' ? 'Median home value' : 'Median monthly rent') : (S.mode === 'value' ? 'Home value' : 'Monthly rent');
  const head = `<div class="lhead" aria-hidden="true"><span></span><span id="legendTitle">${label}</span><span>Share of US homes</span></div>`;
  const rows = shownClasses().map(i => {
    const lab = cls[i][0];
    const off = S.hidden[S.mode].has(i);
    const share = tot ? us[i] / tot : 0;
    const shareTxt = share >= 0.095 ? Math.round(share * 100) : (share * 100).toFixed(1);
    return `<button class="lrow${off ? ' off' : ''}" data-i="${i}" aria-pressed="${!off}" aria-label="${esc(lab)}, ${shareTxt}% of US ${S.mode === 'value' ? 'owner-occupied' : 'rented'} homes">
      <span class="sw" style="background:${col[i]}"></span><span class="lb">${esc(lab)}</span>
      <span class="bar"><i style="width:${(100 * (us.length ? us[i] / max : 0)).toFixed(1)}%"></i></span><span class="pc">${shareTxt}%</span></button>`;
  }).join('');
  $('#legend').innerHTML = head + rows;
  $('#legend').setAttribute('aria-label', `${label}. Each button hides or shows a price range.`);
  $('#showAll').hidden = !shownClasses().some(i => S.hidden[S.mode].has(i));
  if (focusI != null) { const r = $(`#legend .lrow[data-i="${focusI}"]`); if (r) r.focus(); }
}
function toggleClass(i) {
  const h = S.hidden[S.mode];
  if (h.has(i)) h.delete(i); else h.add(i);
  if (shownClasses().every(k => h.has(k))) h.clear();     // hiding everything means "show everything"
  applyMode();
}
function isolateClass(i, before) {
  const h = S.hidden[S.mode], shown = shownClasses();
  const wasIsolated = shown.every(k => k === i ? !before.has(k) : before.has(k));
  h.clear();
  if (!wasIsolated) classes().forEach((_, k) => { if (k !== i) h.add(k); });
  applyMode();
}
// A click toggles at once; a double-click undoes those two toggles and isolates the class.
let beforeClick = null;
$('#legend').addEventListener('click', e => {
  const row = e.target.closest('.lrow'); if (!row) return;
  if (e.detail <= 1) beforeClick = new Set(S.hidden[S.mode]);
  if (e.detail > 1) return;
  toggleClass(+row.dataset.i);
});
$('#legend').addEventListener('dblclick', e => {
  const row = e.target.closest('.lrow'); if (!row) return;
  isolateClass(+row.dataset.i, beforeClick || new Set(S.hidden[S.mode]));
});
$('#legend').addEventListener('keydown', e => {
  const row = e.target.closest('.lrow'); if (!row) return;
  if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); isolateClass(+row.dataset.i, new Set(S.hidden[S.mode])); }
});
$('#showAll').addEventListener('click', () => { S.hidden[S.mode].clear(); applyMode(); $('#legend .lrow')?.focus(); });
$('#legendHint').textContent = IS_TOUCH ? 'Tap a price to hide or show it' : 'Click to hide · double-click (or Shift+Enter) to isolate';

// ---------------------------------------------------------------- dot ratio
function tierAt(z) {
  let cur = M.tiers[0];
  for (const t of M.tiers) if (z >= t.show[0] + FADE / 2) cur = t;
  return cur;
}
function updateRatio() {
  const z = map.getZoom();
  const s = S.view === 'areas'
    ? 'Median of each ' + (z < 8 ? 'county' : z < 10 ? 'census tract' : 'block group')
    : `Each dot is ${tierAt(z).ratio.toLocaleString('en-US')} homes`;
  const el = $('#ratio');
  if (el.textContent !== s) el.textContent = s;
}

// ---------------------------------------------------------------- hover & tooltip
const tip = $('#tip');
let hoverId = null, hoverLevel = null;
function hitLayers() { return LEVELS.map(l => `af-${l.key}`); }
function setHover(level, id) {
  if (id === hoverId && level === hoverLevel) return;
  if (hoverLevel) map.setFilter(`ah-${hoverLevel}`, ['==', ['get', 'id'], '']);
  hoverId = id; hoverLevel = level;
  if (level) map.setFilter(`ah-${level}`, ['==', ['get', 'id'], id]);
}
function tipHtml(level, p) {
  const title = areaTitle(level, p.id), where = areaWhere(level, p.id);
  if (p.o == null && p.r == null) {
    return `<div class="tt">${esc(title)}</div>${where ? `<div class="ts">${esc(where)}</div>` : ''}<div class="th">No Census estimate for this area</div>`;
  }
  const v = p.v != null ? (p.ve ? '≈' : '') + money(p.v, 'value') : '—';
  const g = p.g != null ? (p.ge ? '≈' : '') + money(p.g, 'rent') : '—';
  return `<div class="tt">${esc(title)}</div>${where ? `<div class="ts">${esc(where)}</div>` : ''}
    <div class="kv"><span>Median home value</span><span>${v}</span><span>Median rent</span><span>${g}</span>
    <span>Owner homes</span><span>${p.x ? '≈' : ''}${n0(+p.o || 0)}</span><span>Rented homes</span><span>${p.x ? '≈' : ''}${n0(+p.r || 0)}</span></div>
    <div class="th">${p.x ? 'Estimated from county totals · ' : ''}Click for details</div>`;
}
function featureAt(point) {
  if (!hitVisible()) return null;
  const fs = map.queryRenderedFeatures(point, { layers: hitLayers().filter(id => map.getLayer(id)) });
  if (!fs.length) return null;
  const f = fs[0];
  return { level: f.layer.id.slice(3), props: f.properties };
}
let raf = 0, lastEvt = null;
function onMove(e) {
  lastEvt = e;
  if (!hitReady) revealHit();
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = 0;
    const e = lastEvt; if (!e) return;
    const hit = featureAt(e.point);
    if (!hit || !hit.props.id) { setHover(null, null); tip.hidden = true; map.getCanvas().style.cursor = ''; return; }
    setHover(hit.level, hit.props.id);
    map.getCanvas().style.cursor = 'pointer';
    tip.innerHTML = tipHtml(hit.level, hit.props);
    tip.hidden = false;
    const r = map.getContainer().getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.point.x + 14, y = e.point.y + 14;
    if (x + w > r.width - 8) x = e.point.x - w - 14;
    if (y + h > r.height - 8) y = e.point.y - h - 14;
    tip.style.transform = `translate(${Math.max(4, x)}px, ${Math.max(4, y)}px)`;
  });
}
// The invisible area layers used for hover/click are only switched on after the dots have
// drawn (or on first interaction), so their tiles don't compete with the dots for bandwidth.
let revealedAt = 0;
function revealHit() {
  if (hitReady) return;
  hitReady = true; revealedAt = performance.now();
  setHitVisibility();
}
// Find the area at a screen point, waiting (up to 20 s) for its tiles if they are still loading.
// Right after the hit layers switch on, a source with nothing requested yet reports "loaded",
// so "loaded" only counts a few seconds after that. A newer pick cancels an older one.
let pickTok = 0;
function pickArea(point, cb) {
  const tok = ++pickTok, t0 = performance.now();
  revealHit();
  const check = () => {
    if (tok !== pickTok) return;
    const h = featureAt(point);
    const z = map.getZoom(), lvl = LEVELS.find(l => z >= l.minzoom && z < l.maxzoom) || LEVELS[2];
    const settled = performance.now() - revealedAt > 3000 && map.isSourceLoaded(`a-${lvl.key}`);
    if (h || settled || performance.now() - t0 > 20000) cb(h && h.props.id ? h : null);
    else setTimeout(check, 200);
  };
  check();
}
function setHitVisibility() {
  const v = hitVisible() ? 'visible' : 'none';
  for (const l of LEVELS) for (const p of ['af', 'ah', 'as-case', 'as']) map.setLayoutProperty(`${p}-${l.key}`, 'visibility', v);
}

// ---------------------------------------------------------------- detail panel
const shardCache = new Map();
function shard(cid) {
  if (!shardCache.has(cid)) {
    const p = fetch(`data/areas/${cid}.json`).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .catch(err => { shardCache.delete(cid); throw err; });
    shardCache.set(cid, p);
  }
  return shardCache.get(cid);
}
function rec(arr) { const o = {}; SUM.fields.forEach((f, i) => { o[f] = arr[i]; }); return o; }
async function getRecord(level, id) {
  await sumP;
  if (level === 'us') return rec(SUM.us);
  if (level === 's') return SUM.state[id] ? rec(SUM.state[id][2]) : null;
  if (level === 'c') return SUM.county[id] ? rec(SUM.county[id][1]) : null;
  const sh = await shard(id.slice(0, 5));
  const r = sh[level === 't' ? 'tract' : 'bg'][id];
  return r ? rec(r) : null;
}
function chain(level, id) {
  const out = [];
  if (level === 'b') out.push(['b', id]);
  if (level === 'b' || level === 't') out.push(['t', id.slice(0, 11)]);
  if (level !== 'us' && level !== 's') out.push(['c', id.slice(0, 5)]);
  if (level !== 'us') out.push(['s', id.slice(0, 2)]);
  out.push(['us', 'us']);
  return out;
}
const CRUMB = { b: 'Block group', t: 'Tract', c: 'County', s: 'State', us: 'US' };
const LEVEL_ORDER = ['us', 's', 'c', 't', 'b'];
function distHtml(mode, r) {
  const vals = r[mode === 'value' ? 'value_classes' : 'rent_classes'] || [];
  const tot = vals.reduce((a, b) => a + b, 0);
  if (!tot) return `<div class="note">No ${mode === 'value' ? 'owner-occupied' : 'rented'} homes here.</div>`;
  const cls = classes(mode), col = colors(mode), max = Math.max(...vals);
  const medK = classOf(mode, r[mode]);
  return `<div class="dist" role="list" aria-label="${mode === 'value' ? 'Home values' : 'Monthly rents'}">` + cls.map(([label], i) => {
    const med = i === medK && label !== 'No cash rent';
    return `<div class="drow${med ? ' med' : ''}" role="listitem"><span class="dl">${esc(label)}${med ? '<span class="mk"> · median</span>' : ''}</span>
    <span class="db" aria-hidden="true"><i style="width:${(100 * vals[i] / max).toFixed(1)}%;background:${col[i]}"></i></span><span class="dp">${pct(vals[i], tot)}</span></div>`;
  }).join('') + '</div>';
}
let detailToken = 0;
async function renderDetail(opts = {}) {
  const box = $('#detailBody');
  const { level, id } = S.sel;
  const o = S.sel.origin || S.sel;
  const token = ++detailToken;
  const scroll = opts.keepScroll ? $('#detail').scrollTop : 0;
  $('#detail').hidden = false;
  document.body.classList.add('detail-open');
  if (small()) collapsePanel(true);
  setSelected();
  const ch = chain(o.level, o.id);
  const crumbs = () => ch.map(([l, i]) => `<button data-l="${l}" data-id="${i}" aria-current="${l === level}">${CRUMB[l]}</button>`).join('');
  const head = () => `<div class="crumbs" role="group" aria-label="Zoom out to">${crumbs()}</div>
    <h2 tabindex="-1">${esc(areaTitle(level, id))}</h2><div class="where">${esc(areaWhere(level, id))}</div>`;
  const wire = () => $$('.crumbs button', box).forEach(b => b.addEventListener('click', () => select(b.dataset.l, b.dataset.id, { fly: true, origin: o, focus: true })));
  const loading = setTimeout(() => { if (token === detailToken) { box.innerHTML = head() + `<div class="loadingtxt">Loading…</div>`; wire(); } }, 120);
  let parents, r;
  try {
    const all = await Promise.all(ch.map(([l, i]) => getRecord(l, i)));
    if (token !== detailToken) return;
    parents = ch.map((c, k) => ({ level: c[0], id: c[1], r: all[k] }));
    r = (parents.find(p => p.level === level && p.id === id) || {}).r;
  } catch (err) {
    clearTimeout(loading);
    if (token === detailToken) { box.innerHTML = head() + `<div class="loadingtxt">Couldn't load this area's data. Check your connection and try again.</div>`; wire(); }
    return;
  }
  clearTimeout(loading);
  if (!r) { box.innerHTML = head() + `<div class="loadingtxt">The Census published no estimates for this area.</div>`; wire(); return; }
  const occ = (r.owners || 0) + (r.renters || 0);
  const est = flag => flag ? '≈' : '';
  const moe = m => m ? `± ${dollars(m)}` : '';
  // Ratios of top- or bottom-coded medians are only bounds.
  const vTop = isTop(r.value, 'value'), rTop = isTop(r.rent, 'rent'), iTop = isTop(r.income, 'income');
  const vLow = isBottom(r.value, 'value'), rLow = isBottom(r.rent, 'rent'), iLow = isBottom(r.income, 'income');
  const ratio = (num, den, numHi, numLo, denHi, denLo, f) => {
    if (!num || !den) return '—';
    const up = numHi || denLo, down = numLo || denHi;
    if (up && down) return '—';
    return (up ? 'over ' : down ? 'under ' : '') + f(num / den);
  };
  const pti = ratio(r.value, r.income, vTop, vLow, iTop, iLow, x => x.toFixed(1) + '×');
  const ptr = ratio(r.value, r.rent && 12 * r.rent, vTop, vLow, rTop, rLow, x => x.toFixed(1));
  const rows = parents.filter(p => p.r && LEVEL_ORDER.indexOf(p.level) <= LEVEL_ORDER.indexOf(level)).map(p => `<tr class="${p.level === level ? 'cur' : ''}"><td>${esc(areaTitle(p.level, p.id))}</td>
    <td>${est(p.r.value_est)}${money(p.r.value, 'value')}</td><td>${est(p.r.rent_est)}${money(p.r.rent, 'rent')}</td><td>${money(p.r.income, 'income')}</td></tr>`).join('');
  const synth = r.synth ? `<div class="note warn">The Census Bureau published no estimates for this ${level === 'b' ? 'block group' : 'tract'}. These figures are its share of the county total left over after the published block groups, split by 2020 housing units, so treat them as rough.</div>` : '';
  box.innerHTML = head() + synth + `
    <div class="tiles">
      <div class="tile"><div class="k">Median home value</div><div class="v">${est(r.value_est)}${money(r.value, 'value')}</div><div class="m">${r.value_est ? 'estimated from brackets' : moe(r.value_moe)}</div></div>
      <div class="tile"><div class="k">Median monthly rent</div><div class="v">${est(r.rent_est)}${money(r.rent, 'rent')}</div><div class="m">${r.rent_est ? 'estimated from brackets' : moe(r.rent_moe)}</div></div>
      <div class="tile"><div class="k">Median household income</div><div class="v">${money(r.income, 'income')}</div><div class="m">${moe(r.income_moe)}</div></div>
      <div class="tile"><div class="k">Occupied homes</div><div class="v">${est(r.synth)}${n0(occ)}</div><div class="m">${pct(r.owners, occ)} owned · ${pct(r.renters, occ)} rented</div></div>
    </div>
    <div class="tiles">
      <div class="tile"><div class="k">Value ÷ income</div><div class="v">${pti}</div><div class="m">years of median income</div></div>
      <div class="tile"><div class="k">Rent as share of income</div><div class="v">${burden(r.rent_burden)}</div><div class="m">median for renters</div></div>
      <div class="tile"><div class="k">Price-to-rent ratio</div><div class="v">${ptr}</div><div class="m">value ÷ annual rent</div></div>
      <div class="tile"><div class="k">Vacant homes</div><div class="v">${pct(r.vacant, r.units)}</div><div class="m">of ${n0(r.units)} housing units</div></div>
    </div>
    <h3 class="sec">Home values</h3>${distHtml('value', r)}
    <h3 class="sec">Monthly rents</h3>${distHtml('rent', r)}
    <h3 class="sec">Compared with</h3>
    <table class="cmp"><thead><tr><th scope="col"><span class="sr">Area</span></th><th scope="col">Value</th><th scope="col">Rent</th><th scope="col">Income</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="note">${level === 'b' ? 'Block-group estimates come from small survey samples; margins of error are wide. ' : ''}ACS ${esc(M.acs.replace(' ACS 5-year estimates', ''))} 5-year estimates, 2024 dollars. Values are owner estimates; rent includes utilities.</div>`;
  wire();
  $('#detail').scrollTop = scroll;
  if (opts.focus) $('#detail h2').focus({ preventScroll: true });
}
function setSelected() {
  for (const l of LEVELS) {
    const f = S.sel && S.sel.level === l.key ? ['==', ['get', 'id'], S.sel.id] : ['==', ['get', 'id'], ''];
    map.setFilter(`as-${l.key}`, f); map.setFilter(`as-case-${l.key}`, f);
  }
}
let opener = null;
function select(level, id, opts = {}) {
  if (!S.sel && document.activeElement && document.activeElement !== document.body) opener = document.activeElement;
  S.sel = { level, id, origin: opts.origin || { level, id, lngLat: opts.lngLat } };
  const done = renderDetail({ focus: opts.focus });
  if (opts.fly) flyToArea(level, id);
  return done;
}
function closeDetail() {
  const hadFocus = $('#detail').contains(document.activeElement);
  S.sel = null; detailToken++;
  $('#detail').hidden = true;
  document.body.classList.remove('detail-open');
  setSelected();
  if (hadFocus) (opener && document.contains(opener) ? opener : map.getCanvas()).focus();
  opener = null;
}
$('#detailClose').addEventListener('click', closeDetail);
function flyToArea(level, id) {
  if (level === 'us') return home();
  if (level === 's') {
    if (id === '02') return map.fitBounds(JUMPS.ak, { padding: pad(), duration: 900 });
    if (id === '15') return map.fitBounds(JUMPS.hi, { padding: pad(), duration: 900 });
    loadSearch().then(() => {
      const it = searchIndex && searchIndex.find(x => x[6] === 's' && x[7] === id);
      if (it && it[5]) map.fitBounds([[it[5][0], it[5][1]], [it[5][2], it[5][3]]], { padding: pad(), maxZoom: 7, duration: 900 });
    }).catch(() => {});
    return;
  }
  // keep the zoom inside the range where this level's outline is drawn, and the selected spot in view
  const l = levelOf(level), z = map.getZoom();
  const target = level === 'c' ? 7.2 : level === 't' ? 9.2 : 11.2;
  const ll = S.sel && S.sel.origin && S.sel.origin.lngLat;
  const offscreen = ll && !map.getBounds().contains(ll);
  const zoomOut = z < l.minzoom || z >= l.maxzoom || (level === 'c' && z < 5);
  if (zoomOut || offscreen) map.easeTo({ zoom: zoomOut ? target : z, center: ll || map.getCenter(), offset: ll ? visibleOffset() : [0, 0], duration: 700 });
}

// ---------------------------------------------------------------- search
let searchIndex = null, searchNorm = null, searchLoading = null;
const ALIAS = [[/\bsaint\b/g, 'st'], [/\bsainte\b/g, 'ste'], [/\bmount\b/g, 'mt'], [/\bfort\b/g, 'ft']];
function norm(s) {
  let n = s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
  for (const [re, to] of ALIAS) n = n.replace(re, to);
  return n;
}
function loadSearch() {
  if (!searchLoading) searchLoading = fetch('data/search.json').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(d => {
    searchIndex = d; searchNorm = d.map(x => norm(x[0]));
  }).catch(err => { searchLoading = null; throw err; });
  return searchLoading;
}
// search entries: [name, state | 'State' | 'ZIP', lat, lon, zoom, bbox or 0, kind, GEOID?, note?]
const KINDS = { s: 'State', c: 'County', p: 'Place', z: 'ZIP code' };
function kindOf(it) { return KINDS[it[6]] || 'Place'; }
function stateFilter(q) {
  if (!q || !SUM) return null;
  const ents = Object.values(SUM.state);
  return ents.find(s => norm(s[0]) === q) || ents.find(s => norm(s[1]) === q) || ents.find(s => norm(s[1]).startsWith(q)) || null;
}
// "40.7, -74.0" style input
function parseLatLng(q) {
  const m = q.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (!m || !/[.,-]/.test(q)) return null;
  let a = +m[1], b = +m[2];
  if (Math.abs(a) > 90 && Math.abs(b) <= 90) [a, b] = [b, a];       // "lng, lat"
  if (Math.abs(a) > 85 || Math.abs(b) > 180) return null;
  return { lat: a, lng: b };
}
function search(q) {
  if (!norm(q) || !searchIndex) return [];
  // "Springfield, IL" / "Portland, oregon": the part after a comma narrows by state
  const comma = q.includes(',') ? norm(q.split(',').slice(1).join(' ')) : '';
  const namePart = norm(q.split(',')[0]);
  if (!namePart) return [];
  const st = stateFilter(comma);
  if (comma && !st) return [];
  const out = [[], []];
  for (let i = 0; i < searchIndex.length && out[0].length < 8; i++) {
    const it = searchIndex[i], n = searchNorm[i];
    if (st && it[1] !== st[0] && !(it[1] === 'State' && it[0] === st[1])) continue;
    if (n.startsWith(namePart)) out[0].push(it);
    else if (out[1].length < 8 && n.includes(' ' + namePart)) out[1].push(it);
  }
  return [...out[0], ...out[1]].slice(0, 8);
}
let results = [], active = -1;
const qEl = $('#q'), resEl = $('#results'), statusEl = $('#searchStatus');
function setExpanded(on) {
  qEl.setAttribute('aria-expanded', String(on)); resEl.hidden = !on;
  if (!on) setActive(-1);
  // phones: the list opens upward from the search box (see style.css)
  resEl.style.bottom = on && small() ? (innerHeight - $('#searchBox').getBoundingClientRect().top + 4) + 'px' : '';
}
function setActive(i) {
  active = i;
  $$('#results li[role="option"]').forEach((li, k) => li.setAttribute('aria-selected', String(k === i)));
  if (i >= 0) { qEl.setAttribute('aria-activedescendant', 'res-' + i); $('#res-' + i)?.scrollIntoView({ block: 'nearest' }); }
  else qEl.removeAttribute('aria-activedescendant');
}
function renderResults(q) {
  results = []; setActive(-1);
  if (!q.trim()) { setExpanded(false); statusEl.textContent = ''; return; }
  const ll = parseLatLng(q);
  if (ll) { setExpanded(false); statusEl.textContent = 'Press Enter to go to these coordinates'; return; }
  if (!searchIndex) { setExpanded(false); statusEl.textContent = 'Loading places…'; return; }
  results = search(q);
  if (!results.length) { setExpanded(false); statusEl.textContent = 'No matches'; return; }
  const nq = norm(q.split(',')[0]);
  resEl.innerHTML = results.map((it, i) => {
    const sub = it[1] === 'State' || it[1] === 'ZIP' ? '' : ', ' + esc(it[1]);
    const shown = norm(it[0]).startsWith(nq) && nq.length <= it[0].length && norm(it[0].slice(0, nq.length)) === nq
      ? `<b>${esc(it[0].slice(0, nq.length))}</b>${esc(it[0].slice(nq.length))}` : esc(it[0]);
    return `<li role="option" id="res-${i}" aria-selected="false" data-i="${i}"><span class="nm">${shown}${sub}</span><span class="kd">${it[8] ? esc(it[8]) : kindOf(it)}</span></li>`;
  }).join('');
  setExpanded(true);
  setActive(0);
  statusEl.textContent = `${results.length} result${results.length > 1 ? 's' : ''}`;
}
// Fly so the target lands in the middle of the visible (unobstructed) part of the map.
function visibleOffset() {
  const p = pad();
  return [(p.left - p.right) / 2, (p.top - p.bottom) / 2];
}
function go(it) {
  setExpanded(false); statusEl.textContent = '';
  qEl.value = it[1] === 'State' || it[1] === 'ZIP' ? it[0] : it[0] + ', ' + it[1];
  if (small()) { qEl.blur(); collapsePanel(true); }
  // a state or county search opens its numbers first, so the framing leaves room for the panel
  const kind = it[6], center = [it[3], it[2]];
  if ((kind === 's' || kind === 'c') && it[7]) select(kind, it[7], { lngLat: kind === 'c' ? center : undefined });
  if (kind === 's') {
    map.fitBounds([[it[5][0], it[5][1]], [it[5][2], it[5][3]]], { padding: pad(), maxZoom: 7.5, duration: 1200 });
  } else {
    map.flyTo({ center, zoom: it[4], duration: 1400, offset: visibleOffset() });
  }
}
qEl.addEventListener('focus', () => loadSearch().then(() => renderResults(qEl.value)).catch(() => { statusEl.textContent = "Couldn't load the place list"; }));
qEl.addEventListener('input', () => loadSearch().then(() => renderResults(qEl.value)).catch(() => {}));
qEl.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); if (!results.length) return;
    setActive((active + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const ll = parseLatLng(qEl.value);
    if (ll) { statusEl.textContent = ''; map.flyTo({ center: [ll.lng, ll.lat], zoom: 13, duration: 1400, offset: visibleOffset() }); return; }
    if (!resEl.hidden && active >= 0 && results[active]) go(results[active]);
  } else if (e.key === 'Escape') {
    e.stopPropagation();
    if (!resEl.hidden) setExpanded(false); else { qEl.value = ''; statusEl.textContent = ''; }
  }
});
resEl.addEventListener('mousedown', e => { const li = e.target.closest('li[data-i]'); if (li) { e.preventDefault(); go(results[+li.dataset.i]); } });
qEl.addEventListener('blur', () => setTimeout(() => setExpanded(false), 150));

// ---------------------------------------------------------------- view / hash
const CONUS = [[-124.8, 24.4], [-66.9, 49.4]];
const JUMPS = { ak: [[-154.5, 55.2], [-130.5, 65.2]], hi: [[-160.3, 18.85], [-154.75, 22.3]], pr: [[-67.3, 17.9], [-65.2, 18.55]] };
function pad() {
  const H = window.innerHeight, W = window.innerWidth;
  if (small()) {
    // the details sheet may still be filling in: assume its full height (see #detail max-height)
    const sheet = !$('#detail').hidden ? Math.max($('#detail').offsetHeight, H * (H < W ? 0.6 : 0.7)) : $('#panel').offsetHeight;
    const top = 50;
    return { top, bottom: Math.max(0, Math.min(sheet + 10, H - top - Math.max(120, 0.3 * H))), left: 16, right: 16 };
  }
  return { top: 40, bottom: 40, left: 340, right: S.sel ? 440 : 70 };
}
// center of the part of the map not covered by panels
function visibleCenter() {
  const p = pad(), r = map.getContainer().getBoundingClientRect();
  return [p.left + (r.width - p.left - p.right) / 2, p.top + (r.height - p.top - p.bottom) / 2];
}
function home() { map.fitBounds(CONUS, { padding: pad(), duration: 1000 }); }
let hashTimer = 0;
function writeHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    if (!map) return;
    const c = map.getCenter().wrap();
    const h = `#${S.mode}${S.view === 'areas' ? '-areas' : ''}/${map.getZoom().toFixed(2)}/${c.lat.toFixed(4)}/${c.lng.toFixed(4)}`;
    if (location.hash !== h) history.replaceState(null, '', h);
  }, 250);
}
function readHash() {
  const m = location.hash.match(/^#(value|rent)(-areas)?(?:\/(-?[\d.]+)\/(-?[\d.]+)\/(-?[\d.]+))?$/);
  if (!m) return null;
  S.mode = m[1]; S.view = m[2] ? 'areas' : 'dots';
  if (!m[3]) return null;
  const zoom = Number(m[3]), lat = Number(m[4]), lng = Number(m[5]);
  if (![zoom, lat, lng].every(Number.isFinite) || Math.abs(lat) > 85 || zoom < 0 || zoom > 22) return null;
  return { zoom, center: [((lng + 540) % 360) - 180, lat] };
}

// ---------------------------------------------------------------- panel controls
$$('#modeSeg button').forEach(b => b.addEventListener('click', () => { S.mode = b.dataset.mode; applyMode(); }));
$$('#viewSeg button').forEach(b => b.addEventListener('click', () => { S.view = b.dataset.view; applyMode(); }));
$$('#palSeg button').forEach(b => b.addEventListener('click', () => { S.pal = b.dataset.pal; try { localStorage.setItem('housing-pal', S.pal); } catch (_) {} applyMode({ colors: true }); }));
$('#labelsToggle').addEventListener('change', e => { S.labels = e.target.checked; applyLabels(); });
for (const seg of ['#modeSeg', '#viewSeg', '#palSeg']) {
  $(seg).addEventListener('keydown', e => {
    const fwd = ['ArrowRight', 'ArrowDown'].includes(e.key), back = ['ArrowLeft', 'ArrowUp'].includes(e.key);
    if (!fwd && !back && e.key !== 'Home' && e.key !== 'End') return;
    e.preventDefault();
    const bs = $$('button', $(seg)); const i = bs.findIndex(b => b.getAttribute('aria-checked') === 'true');
    const nb = e.key === 'Home' ? bs[0] : e.key === 'End' ? bs[bs.length - 1] : bs[(i + (fwd ? 1 : -1) + bs.length) % bs.length];
    nb.click(); nb.focus();
  });
}
function collapsePanel(c) {
  $('#panel').classList.toggle('collapsed', c);
  $('#grip').setAttribute('aria-expanded', String(!c));
  $('#grip').setAttribute('aria-label', c ? 'Expand panel' : 'Collapse panel');
}
$('#grip').addEventListener('click', () => collapsePanel(!$('#panel').classList.contains('collapsed')));
$('#aboutBtn').addEventListener('click', () => $('#about').showModal());
$('#aboutClose').addEventListener('click', () => $('#about').close());
$('#about').addEventListener('click', e => { if (e.target === $('#about')) $('#about').close(); });
$('#zin').addEventListener('click', () => map.zoomIn());
$('#zout').addEventListener('click', () => map.zoomOut());
$('#home').addEventListener('click', home);
$$('.jump button').forEach(b => b.addEventListener('click', () => map.fitBounds(JUMPS[b.dataset.jump], { padding: pad(), duration: 1200 })));
$('#locate').addEventListener('click', () => {
  if (!navigator.geolocation) return;
  $('#locate').disabled = true;
  navigator.geolocation.getCurrentPosition(p => {
    $('#locate').disabled = false;
    map.flyTo({ center: [p.coords.longitude, p.coords.latitude], zoom: 13, duration: 1600, offset: visibleOffset() });
  }, () => { $('#locate').disabled = false; }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 });
});
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== qEl && !$('#about').open && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) { e.preventDefault(); qEl.focus(); }
  if (e.key === 'Escape' && !$('#about').open && S.sel) closeDetail();
});

// ---------------------------------------------------------------- keyboard map selection
// With the map focused from the keyboard, a crosshair marks the center; Enter or Space opens
// the details of the area under it (arrow keys and +/- pan and zoom, as MapLibre provides).
function wireKeyboardMap() {
  const canvas = map.getCanvas();
  canvas.setAttribute('aria-label', 'Map. Arrow keys pan, plus and minus zoom, Enter shows details for the area at the crosshair.');
  const place = () => { const [x, y] = visibleCenter(); const ch = $('#crosshair'); ch.style.left = x + 'px'; ch.style.top = y + 'px'; };
  canvas.addEventListener('focus', () => { if (canvas.matches(':focus-visible')) { place(); $('#crosshair').hidden = false; } });
  addEventListener('resize', place);
  canvas.addEventListener('blur', () => { $('#crosshair').hidden = true; });
  canvas.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    const pt = visibleCenter();
    pickArea(pt, h => { if (h) select(h.level, h.props.id, { focus: true, lngLat: map.unproject(pt).toArray() }); });
  });
}

// ---------------------------------------------------------------- boot
async function boot() {
  try { const p = localStorage.getItem('housing-pal'); if (p && PALETTES[p]) S.pal = p; } catch (_) {}
  const view = readHash();
  const usP = fetch('data/us.json').then(r => r.ok ? r.json() : null).catch(() => null);
  try {
    M = await fetch('data/manifest.json').then(r => { if (!r.ok) throw new Error('manifest ' + r.status); return r.json(); });
  } catch (err) {
    $('#loading').classList.add('err');
    $('#loading > div').textContent = 'The map data could not be loaded. Please try again later.';
    return;
  }
  $('#vintage').textContent = 'ACS ' + M.acs.replace(' ACS 5-year estimates', '');
  $('#builtNote').textContent = `Data built ${M.built}.`;
  if (small()) collapsePanel(true);
  syncSegs();
  map = new maplibregl.Map({
    container: 'map', style: buildStyle(),
    bounds: view ? undefined : CONUS, fitBoundsOptions: { padding: pad() },
    center: view ? view.center : undefined, zoom: view ? view.zoom : undefined,
    minZoom: 1.5, maxZoom: 17.5, attributionControl: false, dragRotate: false, pitchWithRotate: false,
    hash: false, fadeDuration: 150, maxTileCacheZoomLevels: 3,
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  window.__map = map;
  wireKeyboardMap();
  // Show the UI as soon as the style is in place; don't wait for the third-party base map.
  let ready = false;
  const onReady = () => {
    if (ready) return; ready = true;
    renderLegend(); applyMode();
    usP.then(us => { if (us && us.geometry) { US_SHAPE = us.geometry; splitLabels(); } });
    const ld = $('#loading');
    ld.classList.add('done');
    setTimeout(() => { ld.hidden = true; }, 450);
  };
  map.once('style.load', onReady);
  map.once('load', onReady);
  setTimeout(() => { if (map.isStyleLoaded()) onReady(); }, 4000);
  map.once('idle', () => setTimeout(revealHit, 300));
  setTimeout(() => { if (ready) revealHit(); }, 6000);
  map.on('zoom', updateRatio);
  map.on('moveend', writeHash);
  // a hand-edited URL hash jumps there (our own replaceState calls don't fire this)
  addEventListener('hashchange', () => {
    const v = readHash();
    applyMode();
    if (v) map.jumpTo({ center: v.center, zoom: v.zoom });
  });
  if (!IS_TOUCH) {
    map.on('mousemove', onMove);
    map.on('mouseout', () => { lastEvt = null; setHover(null, null); tip.hidden = true; });
    map.on('movestart', () => { tip.hidden = true; });
  }
  map.on('click', e => {
    pickArea(e.point, hit => {
      if (!hit) { if (S.sel) closeDetail(); return; }
      select(hit.level, hit.props.id, { lngLat: e.lngLat.toArray() }).then(() => {
        // on phones the details sheet covers the lower part of the map: keep the tapped spot in view
        if (!small() || $('#detail').hidden) return;
        const sheetTop = $('#detail').getBoundingClientRect().top - map.getContainer().getBoundingClientRect().top;
        const y = map.project(e.lngLat).y;
        if (y > sheetTop - 30) map.panBy([0, y - Math.max(60, sheetTop / 2)], { duration: 400 });
      });
    });
  });
  let busyT = 0;
  map.on('dataloading', () => { clearTimeout(busyT); busyT = setTimeout(() => $('#busy').classList.add('on'), 300); });
  map.on('idle', () => { clearTimeout(busyT); $('#busy').classList.remove('on'); });
  map.on('error', e => { if (e && e.error && !/abort/i.test(String(e.error.message))) console.warn(e.error); });
  sumP.then(() => { if (S.sel) renderDetail({ keepScroll: true }); }).catch(() => {});
}
boot();
})();
