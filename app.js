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
// 15 steps for home values, 14 for rents and monthly costs (which share one scale).
const PALETTES = {
  heat: {
    15: ['#2378c2', '#4674d3', '#656edf', '#8167e5', '#9d5fe5', '#bb59d8', '#d653c3', '#ed50a7', '#f86594', '#fc7b87', '#fc9182', '#feac85', '#fec590', '#ffdda0', '#fff4b2'],
    14: ['#2378c2', '#4973d4', '#696de0', '#8866e6', '#a65de3', '#c656d1', '#e151b7', '#f35b9c', '#fb728b', '#fd8a83', '#fea582', '#fec28e', '#ffdb9e', '#fff4b2'],
  },
  cvd: {
    15: ['#426ec2', '#2e7cc5', '#0b89c5', '#0295bf', '#0d9fba', '#03aab6', '#10b6ac', '#03c49d', '#3acf84', '#6bd66e', '#91dd5c', '#b4e345', '#d2e844', '#e9ee5b', '#fef473'],
    14: ['#426ec2', '#2c7dc5', '#018bc5', '#1197bd', '#11a2b8', '#04afb2', '#17bca7', '#10cb90', '#5cd374', '#86db62', '#ace24b', '#cee740', '#e8ed59', '#fef473'],
  },
};
const NO_CASH = '#7b808a';

// ---------------------------------------------------------------- state
const S = {
  mode: 'cost',             // 'cost' | 'value' | 'rent'
  rent: 'market',           // rents: 'market' (if let today) | 'paid' (what tenants pay)
  who: 'all',               // monthly cost: 'all' | 'owners' (buying) | 'renters'
  size: 'all',              // 'all' or bedrooms '0'..'4': ZIP codes by typical price for that size
  view: 'dots',             // 'dots' | 'areas'
  pal: 'heat',
  labels: true,
  hidden: { v: new Set(), p: new Set(), m: new Set(), c: new Set() },   // hidden class indices
  sel: null,                // { level: 'c'|'t'|'b'|'s'|'us'|'z', id, props, origin: {level, id, lngLat} }
};
let M = null;               // manifest
let SUM = null;             // summary.json (fetched once the map is idle, or when first needed)
let map = null;
let hitReady = false;       // area hit-test tiles are deferred until the dots have drawn
const FADE = 0.6;           // zoom range over which dot tiers cross-fade
const RENTER = 16;          // class offset of renters in the monthly-cost tiles (pipeline/05_tiles.py)
let sumP = null;
function loadSummary() {
  if (!sumP) {
    sumP = fetch('data/summary.json').then(r => { if (!r.ok) throw new Error('summary ' + r.status); return r.json(); })
      .then(d => { SUM = d; return d; });
    sumP.catch(() => { sumP = null; });
  }
  return sumP;
}

// Datasets: v home values (owners), p rent paid and m market rent (renters), c monthly cost (everyone).
const DKEYS = ['v', 'p', 'm', 'c'];
const dkey = () => S.mode === 'value' ? 'v' : S.mode === 'rent' ? (S.rent === 'paid' ? 'p' : 'm') : 'c';
const sizeView = () => S.size !== 'all';
// ZIP property shown in the size view: v<b> Zillow value, m<b> HUD market rent, p<b> ACS rent paid, c<b> cost to buy
function sizeField() {
  const b = S.size;
  if (S.mode === 'value') return 'v' + b;
  if (S.mode === 'rent') return (S.rent === 'paid' ? 'p' : 'm') + b;
  return (S.who === 'renters' ? 'm' : 'c') + b;
}
// Zillow publishes no studio home values
const sizeAvailable = (b, mode = S.mode, who = S.who) => b !== '0' || mode === 'rent' || (mode === 'cost' && who === 'renters');
// class scale in use (legend, colors, hidden classes): 'v' value classes, else monthly classes ('p' adds
// "No cash rent"). The size view shares its dataset's scale, so hidden ranges carry across views.
const ckey = () => dkey();
// median property colored in the county / tract / block-group view
function areaField() { return S.mode === 'cost' ? (S.who === 'owners' ? 'co' : S.who === 'renters' ? 'm' : 'c') : dkey(); }

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
  // hp://dots/<tier>/<v|p|m|c>/z/x/y   or   hp://areas/<set>/z/x/y
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
function money(v) {
  if (v == null || !isFinite(v)) return '—';
  if (v >= 9995000) return '$' + (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (v >= 999500) return '$' + (v / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (v >= 1e4) return '$' + Math.round(v / 1e3) + 'K';
  return '$' + Math.round(v).toLocaleString('en-US');
}
const monthly = v => v == null || !isFinite(v) ? '—' : '$' + Math.round(v).toLocaleString('en-US');
function burden(b) {
  if (b == null) return '—';
  if (b >= 50.05) return '50%+';
  if (b <= 9.95) return '10% or less';
  return b.toFixed(1) + '%';
}
const n0 = v => v == null ? '—' : Math.round(v).toLocaleString('en-US');
const pct = (a, b) => b > 0 ? Math.round(100 * a / b) + '%' : '—';
const times = (a, b) => a > 0 && b > 0 ? (a / b).toFixed(1) + '×' : '—';
function monthName(ym) {
  const [y, m] = String(ym || '').split('-').map(Number);
  return y && m ? new Date(Date.UTC(y, m - 1, 15)).toLocaleString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' }) : '';
}
function dayName(d) {
  const t = new Date(d + 'T12:00:00Z');
  return isNaN(t) ? d : t.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
}
const rateTxt = () => (100 * M.vintage.mortgage_rate).toFixed(2) + '%';
const sizeName = b => b === '0' ? 'studio' : `${b}-bedroom`;
const sizeShort = b => b === '0' ? 'Studio' : `${b} bd`;
function tractName(id) {
  const t = id.slice(5, 11);
  const main = String(parseInt(t.slice(0, 4), 10)), suf = t.slice(4);
  return 'Tract ' + main + (suf !== '00' ? '.' + suf : '');
}
const stateRow = id => SUM && SUM.state[id.slice(0, 2)];
function stateAbbr(id) { const s = stateRow(id); return s ? s[0] : ''; }
function stateName(id) { const s = stateRow(id); return s ? s[1] : ''; }
function countyName(id) {
  const c = id && SUM && SUM.county[id.slice(0, 5)];
  return c ? c[0] + ', ' + stateAbbr(id) : '';
}
function areaTitle(level, id) {
  if (level === 'us') return 'United States';
  if (level === 's') return stateName(id) || 'State';
  if (level === 'c') return countyName(id) || 'County';
  if (level === 't') return tractName(id);
  if (level === 'z') return 'ZIP code ' + id;
  return 'Block group ' + id.slice(11);
}
function areaWhere(level, id, props) {
  if (level === 'b') return tractName(id) + (countyName(id) ? ' · ' + countyName(id) : '');
  if (level === 't') return countyName(id);
  if (level === 'c') return stateName(id);
  if (level === 's') return 'State';
  if (level === 'z') return props && props.cn ? (countyName(props.cn) ? 'Mostly in ' + countyName(props.cn) : '') : '';
  return '';
}

// ---------------------------------------------------------------- classes & colors
const labels = (k = ckey()) => M.classes[k].labels;
const edges = (k = ckey()) => M.classes[k].edges;
function colors(k = ckey()) {
  const ramp = PALETTES[S.pal][k === 'v' ? 15 : 14];
  return k === 'p' ? [...ramp, NO_CASH] : ramp;
}
// legend rows in the current view ("No cash rent" only for paid-rent dots)
function shownClasses(k = ckey()) {
  const n = labels(k).length;
  const noCash = k === 'p' && (S.view === 'areas' || sizeView());
  return [...Array(n).keys()].filter(i => !(noCash && i === n - 1));
}
function classOf(k, v) {
  if (v == null || !isFinite(v)) return -1;
  const e = edges(k); let c = 0;
  for (let i = 0; i < e.length; i++) if (v >= e[i]) c = i;
  return c;
}
// MapLibre expressions: dot class -> color, and which classes are drawn
function dotColorExpr(k) {
  const expr = ['match', ['get', 'k']];
  colors(k).forEach((c, i) => expr.push(k === 'c' ? [i, i + RENTER] : i, c));
  expr.push('#000');
  return expr;
}
function visibleKs(k) {
  const hid = S.hidden[k];
  const ks = [...labels(k).keys()].filter(i => !hid.has(i));
  if (k !== 'c') return ks;
  return [...(S.who !== 'renters' ? ks : []), ...(S.who !== 'owners' ? ks.map(i => i + RENTER) : [])];
}
function stepExpr(field, k, none) {
  const e = edges(k), col = colors(k);
  const step = ['step', ['to-number', ['get', field]], col[0]];
  for (let i = 1; i < e.length; i++) step.push(e[i], col[i]);
  return ['case', ['has', field], step, none];
}
function classFilter(field, k, keepMissing) {
  const hid = S.hidden[k];
  if (!hid.size) return keepMissing ? ['has', 'id'] : ['has', field];
  const e = edges(k), conds = ['any'];
  e.forEach((lo, i) => {
    if (hid.has(i)) return;
    const hi = e[i + 1], v = ['to-number', ['get', field]];
    conds.push(hi == null ? ['>=', v, lo] : ['all', ['>=', v, lo], ['<', v, hi]]);
  });
  return keepMissing ? ['any', ['!', ['has', field]], ['all', ['has', field], conds]] : ['all', ['has', field], conds];
}

// ---------------------------------------------------------------- base map style (OpenFreeMap / OpenMapTiles)
const OFM = 'https://tiles.openfreemap.org';
// A mid-dark slate base: light enough that streets, water and place names read at a glance, dark
// enough that the darkest (cheapest) dot color still stands out and the brightest pops.
const P = {
  land: '#2b3038', water: '#1a2636', park: '#2b3832', urban: '#2f343c', mask: '#1c222b',
  road: '#3f4651', roadHi: '#4d5461', motor: '#5b6372', bnd: '#69717f', bnd2: '#8e95a3', building: '#363b44',
  label: '#e9ecf1', label2: '#c9cfd8', halo: 'rgba(22,25,31,0.94)', waterLabel: '#8eaed6',
};
const W = (pairs) => ['interpolate', ['exponential', 1.5], ['zoom'], ...pairs.flat()];
const NAME = ['coalesce', ['get', 'name:en'], ['get', 'name_en'], ['get', 'name']];
function baseLayers() {
  const B = { source: 'base' };
  const cls = (...c) => ['in', ['get', 'class'], ['literal', c]];
  const L = [];
  L.push({ id: 'bg', type: 'background', paint: { 'background-color': P.land } });
  L.push({ ...B, id: 'park', type: 'fill', 'source-layer': 'park', minzoom: 7, paint: { 'fill-color': P.park, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 7, 0, 10, 1] } });
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
      'line-width': roadW([[5, 0.6, 0.5, 0.4, 0.3, 0.3, 0.2, 0.1], [8, 1.1, 0.9, 0.7, 0.5, 0.4, 0.3, 0.1], [12, 2.2, 2, 1.7, 1.4, 1.2, 0.9, 0.5], [15, 6, 5.5, 5, 4.2, 3.6, 2.8, 1.5], [18, 20, 18, 16, 14, 12, 10, 5]]) } });
  L.push({ ...B, id: 'bnd4', type: 'line', 'source-layer': 'boundary', minzoom: 2, filter: ['all', ['==', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd, 'line-width': W([[3, 0.5], [8, 1], [14, 1.6]]) } });
  L.push({ ...B, id: 'bnd2', type: 'line', 'source-layer': 'boundary', filter: ['all', ['==', ['get', 'admin_level'], 2], ['!=', ['get', 'maritime'], 1], ['!=', ['get', 'disputed'], 1]], layout: { 'line-join': 'round' },
    paint: { 'line-color': P.bnd2, 'line-width': W([[2, 0.7], [8, 1.4]]) } });
  return L;
}
// Place names draw under the mask, which dims the ones outside the US (it covers only foreign
// land and open sea), so no per-label geometry test is needed.
function labelLayers() {
  const B = { source: 'base', type: 'symbol' };
  const vis = S.labels ? 'visible' : 'none';
  const font = ['Noto Sans Regular'], bold = ['Noto Sans Bold'];
  const halo = { 'text-halo-color': P.halo, 'text-halo-width': 1.6, 'text-halo-blur': 0.3 };
  return [
    { id: 'lbl-water', 'source-layer': 'water_name', minzoom: 3, filter: ['==', ['geometry-type'], 'Point'],
      layout: { 'text-field': NAME, 'text-font': font, 'text-size': W([[3, 10.5], [10, 13.5]]), 'text-max-width': 6, 'text-letter-spacing': 0.08 },
      paint: { 'text-color': P.waterLabel, ...halo } },
    { id: 'lbl-road', 'source-layer': 'transportation_name', minzoom: 13,
      layout: { 'symbol-placement': 'line', 'text-field': NAME, 'text-font': font, 'text-size': W([[13, 10.5], [17, 13.5]]), 'text-max-angle': 30 },
      paint: { 'text-color': '#b4bbc6', ...halo } },
    { id: 'lbl-place-minor', 'source-layer': 'place', minzoom: 9.5, filter: ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter', 'hamlet', 'village']]],
      layout: { 'text-field': NAME, 'text-font': font, 'text-size': W([[9.5, 10.5], [12, 12.5], [15, 14.5]]), 'text-max-width': 7, 'text-transform': ['case', ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter']]], 'uppercase', 'none'], 'text-letter-spacing': ['case', ['in', ['get', 'class'], ['literal', ['suburb', 'neighbourhood', 'quarter']]], 0.08, 0] },
      paint: { 'text-color': P.label2, ...halo } },
    { id: 'lbl-town', 'source-layer': 'place', minzoom: 7, filter: ['==', ['get', 'class'], 'town'],
      layout: { 'text-field': NAME, 'text-font': font, 'text-size': W([[7, 11], [12, 15]]), 'text-max-width': 7 },
      paint: { 'text-color': P.label, ...halo } },
    { id: 'lbl-city', 'source-layer': 'place', minzoom: 3, filter: ['==', ['get', 'class'], 'city'],
      layout: { 'text-field': NAME, 'text-font': ['step', ['zoom'], ['literal', font], 6, ['literal', bold]], 'text-size': ['interpolate', ['linear'], ['zoom'], 3, ['case', ['<=', ['coalesce', ['get', 'rank'], 9], 3], 12, 10.5], 10, ['case', ['<=', ['coalesce', ['get', 'rank'], 9], 3], 18, 15]],
        'text-max-width': 7, 'symbol-sort-key': ['coalesce', ['get', 'rank'], 99] },
      paint: { 'text-color': '#f6f8fb', ...halo, 'text-halo-width': 1.8 } },
    { id: 'lbl-state', 'source-layer': 'place', minzoom: 3.2, maxzoom: 7, filter: ['==', ['get', 'class'], 'state'],
      layout: { 'text-field': NAME, 'text-font': bold, 'text-size': W([[3, 9.5], [6, 13]]), 'text-transform': 'uppercase', 'text-letter-spacing': 0.14, 'text-max-width': 8 },
      paint: { 'text-color': '#a3aab6', ...halo } },
  ].map(d => ({ ...B, ...d, layout: { ...d.layout, visibility: vis } }));
}

// ---------------------------------------------------------------- data layers
// Base dot radius by zoom; each tier grows its dots by up to 30% across its range so that
// coverage stays even as the next tier (2.5x more dots) takes over.
const R_STOPS = [[0, 0.55], [3, 0.8], [5, 1.0], [7, 1.2], [9, 1.45], [11, 1.75], [12, 2.0], [14, 3.0], [16, 5.2], [18, 10]];
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
const dotVisible = k => S.view === 'dots' && !sizeView() && dkey() === k;
function dotLayers() {
  const L = [];
  const last = M.tiers.length - 1;
  M.tiers.forEach((t, i) => {
    const [s, e] = t.show;
    const stops = i === 0 ? [0, 1] : [s, 0, s + FADE, 1];
    if (i !== last) stops.push(e, 1, e + FADE, 0);
    DKEYS.forEach(k => {
      L.push({
        id: `dot-${t.name}-${k}`, type: 'circle', source: `d-${t.name}-${k}`, 'source-layer': k,
        minzoom: i === 0 ? 0 : s, maxzoom: i === last ? 24 : e + FADE,
        filter: ['in', ['get', 'k'], ['literal', visibleKs(k)]],
        layout: { visibility: dotVisible(k) ? 'visible' : 'none' },
        paint: {
          'circle-color': dotColorExpr(k),
          'circle-radius': tierRadius(t, i === last),
          'circle-opacity': ['interpolate', ['linear'], ['zoom'], ...stops],
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
const ZIPL = { key: 'z', set: 'zcta', minzoom: 0, maxzoom: 24 };     // size view
const AREAS = [...LEVELS, ZIPL];
const levelOf = k => AREAS.find(l => l.key === k);
const hitVisible = () => S.view === 'areas' || hitReady;
const areasOn = () => S.view === 'areas' && !sizeView();
function layerVis(l) { return (l.key === 'z' ? sizeView() : !sizeView() && hitVisible()) ? 'visible' : 'none'; }
function fillPaint(l) {
  if (l.key === 'z') return sizeView() ? stepExpr(sizeField(), ckey(), 'rgba(255,255,255,0.05)') : '#000';
  return areasOn() ? stepExpr(areaField(), dkey(), 'rgba(0,0,0,0)') : '#000';
}
function fillFilter(l) {
  if (l.key === 'z') return sizeView() ? classFilter(sizeField(), ckey(), true) : ['has', 'id'];
  return areasOn() ? classFilter(areaField(), dkey(), true) : ['has', 'id'];     // areas without a median stay clickable
}
const fillOpacity = l => (l.key === 'z' ? sizeView() : areasOn()) ? 0.88 : 0;
function areaFillLayers() {
  return AREAS.map(l => ({
    id: `af-${l.key}`, type: 'fill', source: `a-${l.key}`, 'source-layer': M.areas[l.set].layer, minzoom: l.minzoom, maxzoom: l.maxzoom,
    filter: fillFilter(l), layout: { visibility: layerVis(l) },
    paint: { 'fill-color': fillPaint(l), 'fill-opacity': fillOpacity(l), 'fill-antialias': false },
  }));
}
function areaLineLayers() {
  const L = [];
  for (const l of AREAS) {
    const base = { source: `a-${l.key}`, 'source-layer': M.areas[l.set].layer, minzoom: l.minzoom, maxzoom: l.maxzoom, layout: { 'line-join': 'round' } };
    const outline = l.key === 'z' ? sizeView() : areasOn();
    L.push({ ...base, id: `ao-${l.key}`, type: 'line', layout: { ...base.layout, visibility: outline ? 'visible' : 'none' },
      paint: { 'line-color': 'rgba(10,12,16,0.55)', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.2, 10, 0.6, 14, 1] } });
    const hl = { ...base.layout, visibility: layerVis(l) };
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
  M.tiers.forEach(t => DKEYS.forEach(k => {
    sources[`d-${t.name}-${k}`] = { type: 'vector', tiles: [`hp://dots/${t.name}/${k}/{z}/{x}/{y}`], minzoom: t.minzoom, maxzoom: t.maxzoom };
  }));
  AREAS.forEach(l => { const a = M.areas[l.set]; sources[`a-${l.key}`] = { type: 'vector', tiles: [`hp://areas/${l.set}/{z}/{x}/{y}`], minzoom: a.minzoom, maxzoom: a.maxzoom }; });
  // The mask fills foreign land and open sea with the water color: it darkens Canada and Mexico
  // (and their place names) and matches US waters exactly, so it leaves no halo around coasts.
  const mask = { id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': P.mask, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.72, 9, 0.62, 13, 0.4] } };
  return {
    version: 8,
    glyphs: OFM + '/fonts/{fontstack}/{range}.pbf',
    sources,
    layers: [...baseLayers(), ...areaFillLayers(), ...dotLayers(), ...areaLineLayers(), ...labelLayers(), mask],
  };
}

// ---------------------------------------------------------------- apply state to the map
function applyMode(opts = {}) {
  if (sizeView() && !sizeAvailable(S.size)) S.size = '1';
  // a view can't show every one of its classes hidden (e.g. after isolating "No cash rent" on paid-rent dots)
  const hk = ckey();
  if (shownClasses(hk).every(c => S.hidden[hk].has(c))) S.hidden[hk].clear();
  if (S.sel && (S.sel.level === 'z') !== sizeView()) closeDetail();
  for (const t of M.tiers) for (const k of DKEYS) {
    const id = `dot-${t.name}-${k}`, on = dotVisible(k);
    map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
    if (on) {       // hidden layers are refreshed when they are next shown
      map.setPaintProperty(id, 'circle-color', dotColorExpr(k));
      map.setFilter(id, ['in', ['get', 'k'], ['literal', visibleKs(k)]]);
    }
  }
  setHitVisibility();
  for (const l of AREAS) {
    map.setPaintProperty(`af-${l.key}`, 'fill-color', fillPaint(l));
    map.setPaintProperty(`af-${l.key}`, 'fill-opacity', fillOpacity(l));
    map.setFilter(`af-${l.key}`, fillFilter(l));
    map.setLayoutProperty(`ao-${l.key}`, 'visibility', (l.key === 'z' ? sizeView() : areasOn()) ? 'visible' : 'none');
  }
  renderLegend();
  syncSegs();
  updateRatio();
  const note = noteText();
  if ($('#modeNote').textContent !== note) $('#modeNote').textContent = note;
  if (S.sel && opts.redraw) renderDetail({ keepScroll: true });
  if (tip && !tip.hidden && lastEvt) onMove(lastEvt);
  writeHash();
}
function applyLabels() {
  for (const l of labelLayers()) if (map.getLayer(l.id)) map.setLayoutProperty(l.id, 'visibility', S.labels ? 'visible' : 'none');
  map.setLayoutProperty('road', 'visibility', S.labels ? 'visible' : 'none');
}
// segmented radio groups: checked state, roving tabindex, contextual rows
const SEGS = [['#modeSeg', 'mode'], ['#rentSeg', 'rent'], ['#whoSeg', 'who'], ['#sizeSeg', 'size'], ['#viewSeg', 'view'], ['#palSeg', 'pal']];
function syncSegs() {
  for (const [sel, key] of SEGS) $$(sel + ' button').forEach(b => {
    const on = b.dataset[key] === S[key];
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  $('#rentSeg').hidden = S.mode !== 'rent';
  $('#whoSeg').hidden = S.mode !== 'cost';
  $$('#sizeSeg button').forEach(b => {
    const ok = b.dataset.size === 'all' || sizeAvailable(b.dataset.size);
    b.disabled = !ok;
    b.title = ok ? '' : 'Zillow publishes no home values for studios';
  });
  $('#viewSeg').closest('.optrow').classList.toggle('dim', sizeView());
  $('#viewSeg').title = sizeView() ? 'Choosing one goes back to all sizes' : '';
}
function noteText() {
  const month = monthName(M.vintage.zhvi), buy = `20% down, ${rateTxt()} 30-year fixed, tax and insurance`;
  if (sizeView()) {
    const n = sizeName(S.size), f = sizeField()[0];
    return {
      v: `Typical value of a ${n} home in each ZIP code (Zillow, ${month}).`,
      m: `HUD Fair Market Rent for a ${n} in each ZIP code: what a recent mover pays (40th percentile), utilities included.`,
      p: `Median rent tenants pay for a ${n} in each ZIP code, utilities included (Census, in ${month} dollars).`,
      c: `What buying a typical ${n} home costs a month in each ZIP code: ${buy}.`,
    }[f];
  }
  switch (dkey()) {
    case 'v': return `What owners' homes are worth, in ${month} dollars.`;
    case 'm': return 'What each rental would rent for if it were listed today, utilities included.';
    case 'p': return 'What tenants pay now, utilities included, rent-regulated and long-held leases too.';
    default:
      return S.who === 'owners' ? `What buying each owner-occupied home today would cost a month: ${buy}.`
        : S.who === 'renters' ? 'What each rental would cost a month if let today, utilities included.'
        : `What moving into each home today costs a month: buying it (${buy}) or renting it.`;
  }
}

// ---------------------------------------------------------------- legend
function legendShares() {
  const k = ckey();
  if (sizeView()) return M.sizes[sizeField()] || [];
  if (k === 'c') {
    const o = M.classes.c.us_owners, r = M.classes.c.us_renters;
    return S.who === 'owners' ? o : S.who === 'renters' ? r : o.map((x, i) => x + r[i]);
  }
  return M.classes[k].us;
}
const NAMES = { v: 'Home value', m: 'Market rent', p: 'Rent paid', c: 'Monthly cost', co: 'Cost to buy' };
function legendTitle() {
  if (sizeView()) {
    const n = sizeShort(S.size);
    return { v: `${n} home value`, m: `${n} market rent (HUD)`, p: `${n} rent paid`, c: `${n} cost to buy` }[sizeField()[0]];
  }
  const k = dkey();
  const base = k === 'c' && S.who === 'owners' ? 'Cost to buy' : k === 'c' && S.who === 'renters' ? 'Market rent' : NAMES[k];
  return S.view === 'areas' ? 'Median ' + base.toLowerCase() : base;
}
function shareNoun() {
  if (sizeView()) return 'US homes, by ZIP code';
  return { v: 'US owner-occupied homes', p: 'US rented homes', m: 'US rented homes' }[dkey()] ||
    (S.who === 'owners' ? 'US owner-occupied homes' : S.who === 'renters' ? 'US rented homes' : 'US homes');
}
function renderLegend() {
  const k = ckey(), lab = labels(k), col = colors(k);
  const us = legendShares();
  const shown = shownClasses(k);
  const tot = shown.reduce((a, i) => a + (us[i] || 0), 0);
  const max = Math.max(1, ...shown.map(i => us[i] || 0));
  const focusI = document.activeElement && document.activeElement.closest && document.activeElement.closest('.lrow') ? document.activeElement.closest('.lrow').dataset.i : null;
  const title = legendTitle();
  const head = `<div class="lhead" aria-hidden="true"><span></span><span id="legendTitle">${esc(title)}</span><span>${sizeView() ? 'Share of homes' : 'Share of US homes'}</span></div>`;
  const rows = shown.map(i => {
    const off = S.hidden[k].has(i);
    const share = tot ? (us[i] || 0) / tot : 0;
    const shareTxt = share >= 0.095 ? Math.round(share * 100) : share >= 0.0005 ? (share * 100).toFixed(1) : share > 0 ? '<0.1' : '0';
    return `<button class="lrow${off ? ' off' : ''}" data-i="${i}" aria-pressed="${!off}" aria-label="${esc(lab[i])}, ${shareTxt}% of ${shareNoun()}">
      <span class="sw" style="background:${col[i]}"></span><span class="lb">${esc(lab[i])}</span>
      <span class="bar"><i style="width:${(100 * (us[i] || 0) / max).toFixed(1)}%"></i></span><span class="pc">${shareTxt}%</span></button>`;
  }).join('');
  $('#legend').innerHTML = head + rows;
  $('#legend').setAttribute('aria-label', `${title}. Each button hides or shows a price range.`);
  $('#showAll').hidden = !shown.some(i => S.hidden[k].has(i));
  if (focusI != null) { const r = $(`#legend .lrow[data-i="${focusI}"]`); if (r) r.focus(); }
}
function toggleClass(i) {
  const k = ckey(), h = S.hidden[k];
  if (h.has(i)) h.delete(i); else h.add(i);
  if (shownClasses(k).every(c => h.has(c))) h.clear();     // hiding everything means "show everything"
  applyMode();
}
function isolateClass(i, before) {
  const k = ckey(), h = S.hidden[k], shown = shownClasses(k);
  const wasIsolated = shown.every(c => c === i ? !before.has(c) : before.has(c));
  h.clear();
  if (!wasIsolated) labels(k).forEach((_, c) => { if (c !== i) h.add(c); });
  applyMode();
}
// A click toggles at once; a double-click undoes those two toggles and isolates the class.
let beforeClick = null;
$('#legend').addEventListener('click', e => {
  const row = e.target.closest('.lrow'); if (!row) return;
  if (e.detail <= 1) beforeClick = new Set(S.hidden[ckey()]);
  if (e.detail > 1) return;
  toggleClass(+row.dataset.i);
});
$('#legend').addEventListener('dblclick', e => {
  const row = e.target.closest('.lrow'); if (!row) return;
  isolateClass(+row.dataset.i, beforeClick || new Set(S.hidden[ckey()]));
});
$('#legend').addEventListener('keydown', e => {
  const row = e.target.closest('.lrow'); if (!row) return;
  if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); isolateClass(+row.dataset.i, new Set(S.hidden[ckey()])); }
});
$('#showAll').addEventListener('click', () => { S.hidden[ckey()].clear(); applyMode(); $('#legend .lrow')?.focus(); });
$('#legendHint').textContent = IS_TOUCH ? 'Tap a price to hide or show it' : 'Click to hide · double-click (or Shift+Enter) to isolate';

// ---------------------------------------------------------------- dot ratio
function tierAt(z) {
  let cur = M.tiers[0];
  for (const t of M.tiers) if (z >= t.show[0] + FADE / 2) cur = t;
  return cur;
}
function updateRatio() {
  const z = map.getZoom();
  const s = sizeView() ? (z < M.areas.zcta.minzoom ? 'Zoom in to see ZIP codes' : 'Each ZIP code, by its typical price')
    : S.view === 'areas' ? 'Median of each ' + (z < 8 ? 'county' : z < 10 ? 'census tract' : 'block group')
    : `Each dot is ${tierAt(z).ratio.toLocaleString('en-US')} homes`;
  const el = $('#ratio');
  if (el.textContent !== s) el.textContent = s;
}

// ---------------------------------------------------------------- hover & tooltip
const tip = $('#tip');
let hoverId = null, hoverLevel = null;
const hitLayers = () => sizeView() ? ['af-z'] : LEVELS.map(l => `af-${l.key}`);
function setHover(level, id) {
  if (id === hoverId && level === hoverLevel) return;
  if (hoverLevel) map.setFilter(`ah-${hoverLevel}`, ['==', ['get', 'id'], '']);
  hoverId = id; hoverLevel = level;
  if (level) map.setFilter(`ah-${level}`, ['==', ['get', 'id'], id]);
}
// the row for the dataset on screen is highlighted
function curRow() {
  if (sizeView()) return sizeField()[0];
  const k = dkey();
  return k === 'c' ? (S.who === 'renters' ? 'm' : S.who === 'owners' ? 'co' : 'c') : k;
}
function kvRows(rows) {
  const cur = curRow();
  return `<div class="kv">${rows.map(([k, label, val]) => `<span class="${k === cur ? 'cur' : ''}">${label}</span><span class="${k === cur ? 'cur' : ''}">${val}</span>`).join('')}</div>`;
}
function tipHtml(level, p) {
  const title = areaTitle(level, p.id), where = areaWhere(level, p.id, p);
  const head = `<div class="tt">${esc(title)}</div>${where ? `<div class="ts">${esc(where)}</div>` : ''}`;
  if (level === 'z') {
    const b = S.size, n = sizeName(b);
    const rows = [['v', 'Home value (Zillow)', money(p['v' + b])], ['c', 'Cost to buy, a month', monthly(p['c' + b])],
      ['m', 'Market rent (HUD)', monthly(p['m' + b])], ['p', 'Rent paid (Census)', monthly(p['p' + b])]];
    return head + `<div class="tsec">Typical ${n}${b === '0' ? '' : ' home'}</div>` + kvRows(b === '0' ? rows.slice(2) : rows) + '<div class="th">Click for all sizes</div>';
  }
  const o = +p.o || 0, r = +p.r || 0, occ = o + r, est = p.x ? '≈' : '';
  if (!occ) return head + '<div class="th">No occupied homes here in the Census figures</div>';
  const rows = [['v', 'Home value', money(p.v)], ['m', 'Market rent', monthly(p.m)], ['p', 'Rent paid', monthly(p.p)],
    ['co', 'Cost to buy, a month', monthly(p.co)]];
  if (curRow() === 'c') rows.push(['c', 'Monthly cost, all homes', monthly(p.c)]);
  return head + kvRows(rows) +
    `<div class="tfoot">${est}${n0(occ)} homes · ${pct(o, occ)} owned</div>` +
    `<div class="th">${p.x ? 'Estimated from county totals · ' : ''}Medians · click for details</div>`;
}
function featureAt(point) {
  if (!sizeView() && !hitVisible()) return null;
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
    const z = map.getZoom();
    const src = sizeView() ? 'a-z' : 'a-' + (LEVELS.find(l => z >= l.minzoom && z < l.maxzoom) || LEVELS[2]).key;
    const settled = performance.now() - revealedAt > 3000 && map.isSourceLoaded(src);
    if (h || settled || performance.now() - t0 > 20000) cb(h && h.props.id ? h : null);
    else setTimeout(check, 200);
  };
  check();
}
let hitVis = '';
function setHitVisibility() {
  const now = AREAS.map(layerVis).join();
  // layers just switched on report their (unrequested) sources as loaded: restart pickArea's grace period
  if (now !== hitVis && hitVis) revealedAt = performance.now();
  hitVis = now;
  for (const l of AREAS) for (const p of ['af', 'ah', 'as-case', 'as']) map.setLayoutProperty(`${p}-${l.key}`, 'visibility', layerVis(l));
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
  await loadSummary();
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
const sum = a => (a || []).reduce((x, y) => x + y, 0);
function medTag(on) { return on ? '<span class="mk"> · median</span>' : ''; }
// distribution charts: values (owners), rents (market vs paid), monthly cost (buying vs renting)
function valueChart(r) {
  const vals = r.hv || [], tot = sum(vals);
  if (!tot) return '<div class="note">No owner-occupied homes here.</div>';
  const lab = labels('v'), col = colors('v'), max = Math.max(...vals), medK = classOf('v', r.v);
  return `<div class="dist" role="list" aria-label="Home values">` + lab.map((l, i) => `<div class="drow${i === medK ? ' med' : ''}" role="listitem">
    <span class="dl">${esc(l)}${medTag(i === medK)}</span><span class="db" aria-hidden="true"><i style="width:${(100 * vals[i] / max).toFixed(1)}%;background:${col[i]}"></i></span>
    <span class="dp">${pct(vals[i], tot)}</span></div>`).join('') + '</div>';
}
function rentChart(r) {
  const hm = r.hm || [], hp = r.hp || [], tm = sum(hm), tp = sum(hp);
  if (!tm && !tp) return '<div class="note">No rented homes here.</div>';
  const lab = labels('p'), col = colors('p'), max = Math.max(...hm.map(x => x / (tm || 1)), ...hp.map(x => x / (tp || 1)));
  const km = classOf('m', r.m), kp = classOf('p', r.p), first = S.rent === 'paid' ? 'p' : 'm';
  const bar = (k, i) => {
    const v = k === 'm' ? (hm[i] || 0) / (tm || 1) : (hp[i] || 0) / (tp || 1);
    return `<i class="${k}" style="width:${(100 * v / max).toFixed(1)}%;background:${col[i]}"></i>`;
  };
  return `<div class="dsum">Median market rent ${monthly(r.m)} · median rent paid ${monthly(r.p)}</div>
    <div class="dkey"><span><i class="sw m"></i>Market rent</span><span><i class="sw p"></i>What tenants pay</span></div>
    <div class="dist pair" role="list" aria-label="Monthly rents, market and paid">` + lab.map((l, i) => {
    const nocash = i === lab.length - 1;
    const med = !nocash && (i === km || i === kp);
    const pm = nocash ? '' : pct(hm[i] || 0, tm), pp = pct(hp[i] || 0, tp);
    return `<div class="drow${med ? ' med' : ''}" role="listitem" aria-label="${esc(l)}: ${nocash ? '' : 'market ' + pm + ', '}paid ${pp}">
      <span class="dl">${esc(l)}</span>
      <span class="db two" aria-hidden="true">${nocash ? '' : bar('m', i)}${bar('p', i)}</span>
      <span class="dp">${first === 'm' && !nocash ? pm : pp}</span></div>`;
  }).join('') + '</div>';
}
function costChart(r) {
  const ho = r.hco || [], hr = r.hm || [], tot = sum(ho) + sum(hr);
  if (!tot) return '<div class="note">No homes here.</div>';
  const lab = labels('c'), col = colors('c'), max = Math.max(...lab.map((_, i) => (ho[i] || 0) + (hr[i] || 0)));
  const medK = classOf('c', r.c);
  return `<div class="dsum">Median for all homes: ${monthly(r.c)} a month</div>
    <div class="dkey"><span><i class="sw o"></i>Buying (owner homes)</span><span><i class="sw r"></i>Renting</span></div>
    <div class="dist" role="list" aria-label="Monthly cost, buying and renting">` + lab.map((l, i) => {
    const o = ho[i] || 0, rr = hr[i] || 0;
    return `<div class="drow${i === medK ? ' med' : ''}" role="listitem" aria-label="${esc(l)}: ${pct(o + rr, tot)} of homes, ${pct(o, o + rr)} of them owner homes">
      <span class="dl">${esc(l)}${medTag(i === medK)}</span>
      <span class="db stack" aria-hidden="true"><i class="o" style="width:${(100 * o / max).toFixed(1)}%;background:${col[i]}"></i><i class="r" style="left:${(100 * o / max).toFixed(1)}%;width:${(100 * rr / max).toFixed(1)}%;background:${col[i]}"></i></span>
      <span class="dp">${pct(o + rr, tot)}</span></div>`;
  }).join('') + '</div>';
}
function incomeTxt(r) {
  if (r.income == null) return '—';
  return money(r.income) + (r.income_top ? '+' : '');
}
let detailToken = 0;
function renderDetail(opts = {}) {
  return S.sel.level === 'z' ? renderZip(opts) : renderArea(opts);
}
function openDetail() {
  $('#detail').hidden = false;
  document.body.classList.add('detail-open');
  if (small()) collapsePanel(true);
  setSelected();
}
function sourcesNote(level) {
  const V = M.vintage;
  return `<div class="note">${level === 'b' ? 'Block-group estimates come from small survey samples; margins of error are wide. ' : ''}Prices in ${monthName(V.zhvi)} dollars.
    Values: owners' estimates (Census ACS ${esc(M.acs)}) brought to today with Zillow's home value index; $2M+ homes from 2024–25 mortgage records.
    Market rent: what the home would rent for if let today (recent movers' rents in the Census survey, Zillow's observed rents). Rents include utilities.
    Cost to buy: 20% down, ${V.term_years}-year fixed at ${rateTxt()} (Freddie Mac, ${esc(dayName(V.mortgage_date))}), property tax at the local rate, insurance ${(100 * V.insurance).toFixed(1)}% of value a year.</div>`;
}
async function renderArea(opts) {
  const box = $('#detailBody');
  const { level, id } = S.sel;
  const o = S.sel.origin || S.sel;
  const token = ++detailToken;
  const scroll = opts.keepScroll ? $('#detail').scrollTop : 0;
  openDetail();
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
  if (!r || !((r.owners || 0) + (r.renters || 0))) { box.innerHTML = head() + `<div class="loadingtxt">The Census counted no occupied homes here.</div>`; wire(); return; }
  const occ = (r.owners || 0) + (r.renters || 0);
  const moe = m => m ? `± ${Math.max(1, Math.round(100 * m))}% Census margin` : '';
  const cur = curRow();
  const tile = (k, label, val, sub) => `<div class="tile${k === cur ? ' cur' : ''}"><div class="k">${label}</div><div class="v">${val}</div><div class="m">${sub}</div></div>`;
  const rows = parents.filter(p => p.r && LEVEL_ORDER.indexOf(p.level) <= LEVEL_ORDER.indexOf(level)).map(p => `<tr class="${p.level === level ? 'cur' : ''}"><td>${esc(areaTitle(p.level, p.id))}</td>
    <td>${money(p.r.v)}</td><td>${monthly(p.r.m)}</td><td>${monthly(p.r.p)}</td><td>${monthly(p.r.co)}</td></tr>`).join('');
  const synth = r.synth ? `<div class="note warn">The Census Bureau published no estimates for this ${level === 'b' ? 'block group' : 'tract'}. These figures are its share of the county total left over after the published block groups, split by 2020 housing units, so treat them as rough.</div>` : '';
  const facts = [];
  if (r.rent_burden != null) facts.push(`renters spend a median ${burden(r.rent_burden)} of income on rent`);
  if (r.units) facts.push(`${pct(r.vacant, r.units)} of ${n0(r.units)} homes are vacant`);
  if (r.tax != null) facts.push(`property tax about ${r.tax.toFixed(2)}% of value a year`);
  const gap = r.mratio != null && level === 't' ? Math.round(100 * (r.mratio - 1)) : 0;
  if (gap > 0) facts.push(`recent movers pay about ${gap}% more than the typical renter (before adjusting to Zillow's observed rents)`);
  const charts = { v: ['Home values', valueChart(r)], m: ['Rents', rentChart(r)], c: ['Monthly cost', costChart(r)] };
  const order = dkey() === 'v' ? ['v', 'm', 'c'] : dkey() === 'c' ? ['c', 'v', 'm'] : ['m', 'v', 'c'];
  box.innerHTML = head() + synth + `
    <div class="tiles">
      ${tile('v', 'Median home value', money(r.v), moe(r.v_moe) || "owners' estimates")}
      ${tile('m', 'Market rent', monthly(r.m), 'median, if let today')}
      ${tile('p', 'Rent paid', monthly(r.p), moe(r.p_moe) || 'median, what tenants pay')}
      ${tile('co', 'Cost to buy', monthly(r.co), 'a month, median home')}
    </div>
    <div class="tiles">
      ${tile('', 'Occupied homes', (r.synth ? '≈' : '') + n0(occ), `${pct(r.owners, occ)} owned · ${pct(r.renters, occ)} rented`)}
      ${tile('', 'Buying vs renting', times(r.co, r.m), 'cost to buy ÷ market rent')}
      ${tile('', 'Household income', incomeTxt(r), 'median, a year')}
      ${tile('', 'Price ÷ income', (r.income_top && r.v > 0 ? 'under ' : '') + times(r.v, r.income), 'median value ÷ median income')}
    </div>
    ${facts.length ? `<div class="facts">${esc(facts.join(' · ').replace(/^./, c => c.toUpperCase()))}.</div>` : ''}
    ${order.map(k => `<h3 class="sec">${charts[k][0]}</h3>${charts[k][1]}`).join('')}
    <h3 class="sec">Compared with</h3>
    <table class="cmp"><thead><tr><th scope="col"><span class="sr">Area</span></th><th scope="col">Value</th><th scope="col">Market</th><th scope="col">Paid</th><th scope="col">To buy</th></tr></thead><tbody>${rows}</tbody></table>
    ${sourcesNote(level)}`;
  wire();
  $('#detail').scrollTop = scroll;
  if (opts.focus) $('#detail h2').focus({ preventScroll: true });
}
async function renderZip(opts) {
  const box = $('#detailBody');
  const { id } = S.sel, p = S.sel.props || {};
  const token = ++detailToken;
  const scroll = opts.keepScroll ? $('#detail').scrollTop : 0;
  openDetail();
  const draw = () => {
    if (token !== detailToken) return;
    const sizes = ['0', '1', '2', '3', '4', '5'].filter(b => b !== '5' || p.v5 != null);
    const f = sizeField()[0];
    const td = (k, v) => `<td class="${k === f ? 'cur' : ''}">${v}</td>`;
    const rows = sizes.map(b => `<tr class="${b === S.size ? 'cur' : ''}"><td>${b === '5' ? '5+ bd' : sizeShort(b)}</td>
      ${td('v', b === '0' ? '—' : money(p['v' + b]))}${td('c', b === '0' ? '—' : monthly(p['c' + b]))}${td('m', b === '5' ? '—' : monthly(p['m' + b]))}${td('p', b === '5' ? '—' : monthly(p['p' + b]))}</tr>`).join('');
    const occ = (+p.o || 0) + (+p.r || 0);
    box.innerHTML = `<h2 tabindex="-1">${esc(areaTitle('z', id))}</h2><div class="where">${esc(areaWhere('z', id, p))}</div>
      <h3 class="sec">Typical price by size</h3>
      <table class="cmp sizes-t"><thead><tr><th scope="col"><span class="sr">Size</span></th><th scope="col">Value</th><th scope="col">To buy</th><th scope="col">Market</th><th scope="col">Paid</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="note">Value: Zillow Home Value Index by bedrooms (${monthName(M.vintage.zhvi)}). To buy: a month, ${rateTxt()} 30-year fixed with 20% down, tax and insurance. Market: HUD Fair Market Rent FY${M.vintage.hud_fy}, the 40th percentile of what recent movers pay. Paid: Census median rent tenants pay, in today's dollars. Rents include utilities.</div>
      <h3 class="sec">All homes in this ZIP code</h3>
      <div class="tiles">
        <div class="tile"><div class="k">Median home value</div><div class="v">${money(p.v)}</div><div class="m">Zillow typical: ${money(p.zhvi)}</div></div>
        <div class="tile"><div class="k">Market rent</div><div class="v">${monthly(p.m)}</div><div class="m">Zillow asking: ${monthly(p.zori)}</div></div>
        <div class="tile"><div class="k">Rent paid</div><div class="v">${monthly(p.p)}</div><div class="m">median</div></div>
        <div class="tile"><div class="k">Cost to buy</div><div class="v">${monthly(p.co)}</div><div class="m">a month, median home</div></div>
      </div>
      <div class="facts">${n0(occ)} occupied homes, ${pct(+p.o || 0, occ)} owned. Medians from the block groups mostly inside this ZIP code.</div>
      ${sourcesNote('z')}`;
    $('#detail').scrollTop = scroll;
    if (opts.focus) $('#detail h2').focus({ preventScroll: true });
  };
  draw();
  if (!SUM) loadSummary().then(draw).catch(() => {});
}
function setSelected() {
  for (const l of AREAS) {
    const f = S.sel && S.sel.level === l.key ? ['==', ['get', 'id'], S.sel.id] : ['==', ['get', 'id'], ''];
    map.setFilter(`as-${l.key}`, f); map.setFilter(`as-case-${l.key}`, f);
  }
}
let opener = null;
function select(level, id, opts = {}) {
  if (!S.sel && document.activeElement && document.activeElement !== document.body) opener = document.activeElement;
  S.sel = { level, id, props: opts.props, origin: opts.origin || { level, id, lngLat: opts.lngLat } };
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
  if (level === 'z') return;
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
  if (!searchLoading) searchLoading = Promise.all([fetch('data/search.json').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }), loadSummary().catch(() => null)]).then(([d]) => {
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
  if ((kind === 's' || kind === 'c') && it[7] && !sizeView()) select(kind, it[7], { lngLat: kind === 'c' ? center : undefined });
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
// #<value|rent|cost>[-paid][-buying|-renting][-areas|-studio|-<n>bd]/zoom/lat/lng
let hashTimer = 0;
function writeHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    if (!map) return;
    const c = map.getCenter().wrap();
    const parts = [S.mode];
    if (S.mode === 'rent' && S.rent === 'paid') parts.push('paid');
    if (S.mode === 'cost' && S.who !== 'all') parts.push(S.who === 'owners' ? 'buying' : 'renting');
    if (S.view === 'areas') parts.push('areas');
    if (sizeView()) parts.push(S.size === '0' ? 'studio' : S.size + 'bd');
    const h = `#${parts.join('-')}/${map.getZoom().toFixed(2)}/${c.lat.toFixed(4)}/${c.lng.toFixed(4)}`;
    if (location.hash !== h) history.replaceState(null, '', h);
  }, 250);
}
function readHash() {
  const m = location.hash.match(/^#([a-z0-9-]+)(?:\/(-?[\d.]+)\/(-?[\d.]+)\/(-?[\d.]+))?$/);
  if (!m) return null;
  const tok = m[1].split('-');
  if (!['value', 'rent', 'cost'].includes(tok[0])) return null;
  S.mode = tok[0];
  S.rent = tok.includes('paid') ? 'paid' : 'market';
  S.who = tok.includes('buying') ? 'owners' : tok.includes('renting') ? 'renters' : 'all';
  S.view = tok.includes('areas') ? 'areas' : 'dots';
  const sz = tok.find(t => /^(studio|[1-4]bd)$/.test(t));
  S.size = sz ? (sz === 'studio' ? '0' : sz[0]) : 'all';
  if (!m[2]) return null;
  const zoom = Number(m[2]), lat = Number(m[3]), lng = Number(m[4]);
  if (![zoom, lat, lng].every(Number.isFinite) || Math.abs(lat) > 85 || zoom < 0 || zoom > 22) return null;
  return { zoom, center: [((lng + 540) % 360) - 180, lat] };
}

// ---------------------------------------------------------------- panel controls
const redrawing = { mode: true, rent: true, who: true, size: true };
for (const [sel, key] of SEGS) {
  $$(sel + ' button').forEach(b => b.addEventListener('click', () => {
    if (b.disabled) return;
    // picking Dots or Area medians while a size is shown goes back to all sizes
    if (key === 'view' && sizeView()) S.size = 'all';
    else if (S[key] === b.dataset[key]) return;
    S[key] = b.dataset[key];
    if (key === 'pal') try { localStorage.setItem('housing-pal', S.pal); } catch (_) {}
    applyMode({ redraw: redrawing[key] || key === 'pal' });
  }));
  $(sel).addEventListener('keydown', e => {
    const fwd = ['ArrowRight', 'ArrowDown'].includes(e.key), back = ['ArrowLeft', 'ArrowUp'].includes(e.key);
    if (!fwd && !back && e.key !== 'Home' && e.key !== 'End') return;
    e.preventDefault();
    const bs = $$('button', $(sel)).filter(b => !b.disabled);
    const i = bs.findIndex(b => b.getAttribute('aria-checked') === 'true');
    const nb = e.key === 'Home' ? bs[0] : e.key === 'End' ? bs[bs.length - 1] : bs[(i + (fwd ? 1 : -1) + bs.length) % bs.length];
    nb.click(); nb.focus();
  });
}
$('#labelsToggle').addEventListener('change', e => { S.labels = e.target.checked; applyLabels(); });
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
    pickArea(pt, h => { if (h) select(h.level, h.props.id, { focus: true, lngLat: map.unproject(pt).toArray(), props: h.props }); });
  });
}

// ---------------------------------------------------------------- boot
// Start fetching the first dot archive's header and directory while the map style and base map load.
function prefetch(view) {
  const z = view ? view.zoom : 3.8;
  const t = tierAt(z), files = !sizeView() && S.view === 'dots' ? t.files[dkey()] || [] : [];
  for (const f of files) archive('dots/' + f.file).getHeader().catch(() => {});
}
async function boot() {
  try { const p = localStorage.getItem('housing-pal'); if (p && PALETTES[p]) S.pal = p; } catch (_) {}
  const view = readHash();
  try {
    M = await fetch('data/manifest.json').then(r => { if (!r.ok) throw new Error('manifest ' + r.status); return r.json(); });
  } catch (err) {
    $('#loading').classList.add('err');
    $('#loading > div').textContent = 'The map data could not be loaded. Please try again later.';
    return;
  }
  ZIPL.minzoom = M.areas.zcta.minzoom;
  prefetch(view);
  const month = monthName(M.vintage.zhvi);
  $('#vintage').textContent = 'Prices as of ' + month;
  $('#aboutMonth').textContent = month;
  $('#aboutTerms').textContent = `20% down, a ${M.vintage.term_years}-year fixed mortgage at ${rateTxt()}, the Freddie Mac average for ${dayName(M.vintage.mortgage_date)}`;
  $('#builtNote').textContent = `Data built ${M.built}.`;
  if (small()) collapsePanel(true);
  if (sizeView() && !sizeAvailable(S.size)) S.size = '1';
  syncSegs();
  $('#modeNote').textContent = noteText();
  map = new maplibregl.Map({
    container: 'map', style: buildStyle(),
    bounds: view ? undefined : CONUS, fitBoundsOptions: { padding: pad() },
    center: view ? view.center : undefined, zoom: view ? view.zoom : undefined,
    minZoom: 1.5, maxZoom: 17.5, attributionControl: false, dragRotate: false, pitchWithRotate: false,
    hash: false, fadeDuration: 150, maxTileCacheZoomLevels: 3,
    pixelRatio: Math.min(2, window.devicePixelRatio || 1),     // 3x phones draw 2.25x the pixels for no visible gain
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  window.__map = map;
  wireKeyboardMap();
  // Show the UI as soon as the style is in place; don't wait for the third-party base map.
  let ready = false;
  const onReady = () => {
    if (ready) return; ready = true;
    applyMode();
    const ld = $('#loading');
    ld.classList.add('done');
    setTimeout(() => { ld.hidden = true; }, 450);
  };
  map.once('style.load', onReady);
  map.once('load', onReady);
  setTimeout(() => { if (map.isStyleLoaded()) onReady(); }, 4000);
  map.once('idle', () => setTimeout(() => { revealHit(); loadSummary().catch(() => {}); }, 300));
  setTimeout(() => { if (ready) { revealHit(); loadSummary().catch(() => {}); } }, 6000);
  map.on('zoom', updateRatio);
  map.on('moveend', writeHash);
  // a hand-edited URL hash jumps there (our own replaceState calls don't fire this)
  addEventListener('hashchange', () => {
    const v = readHash();
    applyMode({ redraw: true });
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
      select(hit.level, hit.props.id, { lngLat: e.lngLat.toArray(), props: hit.props }).then(() => {
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
}
boot();
})();
