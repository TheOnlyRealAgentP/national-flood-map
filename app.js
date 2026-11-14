/* National Flood Map -- MapLibre GL JS 5 + PMTiles, no build step.
 *
 * Data:
 *   flood.pmtiles    raster, 8-bit PNG tiles; pixel value = bit mask of flooded return periods
 *                    (bit 0 = 2 yr ... bit 6 = 500 yr, bit 7 = 1000 yr). Colored in the browser.
 *   streams.pmtiles  vector, layer "streams": NWM reaches with discharge and flooded area per period
 *   data/huc8.pmtiles, data/catalog.json, data/stats.json, data/search/*.json  (served with the site)
 */
'use strict';

const LOCAL = ['localhost', '127.0.0.1'].includes(location.hostname);
const CONFIG = {
  // big files: Hugging Face dataset (resolve/main serves range requests with CORS)
  dataBase: LOCAL ? new URL('dist/', location.href).href
                  : 'https://huggingface.co/datasets/HF_USER/national-flood-map-data/resolve/main/',
  hfPage: 'https://huggingface.co/datasets/HF_USER/national-flood-map-data',
  site: new URL('./', location.href).href,
};

const PERIODS = [2, 5, 10, 25, 50, 100, 500];
const CHANCE = { 2: '50%', 5: '20%', 10: '10%', 25: '4%', 50: '2%', 100: '1%', 500: '0.2%' };
// Ordinal blue ramp; the most frequent flood gets the strongest contrast against the basemap.
// Validated (dataviz validateOrdinal) against the basemap land color: every step clears 2:1,
// lightness is monotone and single-hue. Seven classes in one hue cannot reach the 0.06 adjacent
// lightness gap (best possible is 0.047), so the exact periods at any point are also given by
// the click readout, the per-period toggles and single-period mode.
const RAMP = {
  light: ['#0d366b', '#104281', '#184f95', '#1c5cab', '#256abf', '#3987e5', '#6da7ec'],
  dark:  ['#cde2fb', '#9ec5f4', '#86b6ef', '#5598e7', '#3987e5', '#256abf', '#184f95'],
};
const SINGLE = { light: '#1c5cab', dark: '#86b6ef' };
const STATUS = {
  mapped: { label: 'Mapped', color: '#2a78d6' },
  dry:    { label: 'Run, no discharge', color: '#eb6834' },
  notrun: { label: 'Not run (HAND available)', color: '#6250d6' },
  nohand: { label: 'No NOAA HAND', color: '#9a9890' },
};
const STATE_NAMES = {}; // filled from catalog

const state = {
  mode: 'stacked',            // stacked | single
  enabled: new Set(PERIODS),  // stacked mode
  single: 100,                // single mode
  opacity: 0.85,
  base: 'map',
  proj: 'globe',
  theme: 'auto',
  streams: true,
  huc8: true,
  coverage: false,
  selectedHuc: null,
  selectedReach: null,
  highlightName: null,
};

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const store = {
  get(k, d) { try { const v = localStorage.getItem('nfm_' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('nfm_' + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const fmt = (n, d = 0) => n == null || Number.isNaN(n) ? '–' : Number(n).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });
const fmtSig = (n) => n == null || Number.isNaN(n) ? '–' : (Math.abs(n) >= 100 ? fmt(n, 0) : Math.abs(n) >= 10 ? fmt(n, 1) : fmt(n, 2));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const norm = (s) => s.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').trim();

function isDark() {
  if (state.theme === 'dark') return true;
  if (state.theme === 'light') return false;
  return matchMedia('(prefers-color-scheme: dark)').matches;
}
function themeKey() { return isDark() ? 'dark' : 'light'; }

function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, ms);
}

/* ------------------------------------------------------------------ data */
let CATALOG = [], BY_HUC = new Map(), STATS = null, META = null;

async function loadJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

/* ------------------------------------------------------------------ flood tiles */
const pmProtocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', pmProtocol.tile);
const floodPM = new pmtiles.PMTiles(CONFIG.dataBase + 'flood.pmtiles');
const valueCache = new Map(); // "z/x/y" -> Uint8Array(65536) | null
const CACHE_MAX = 800;
let floodVersion = 0;
let LUT = new Uint8ClampedArray(256 * 4);
let EMPTY_PNG = null;

function hexRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)); }

function rebuildLut() {
  const lut = new Uint8ClampedArray(256 * 4);
  const ramp = RAMP[themeKey()];
  for (let v = 1; v < 256; v++) {
    let col = null;
    if (state.mode === 'single') {
      const b = PERIODS.indexOf(state.single);
      if (v >> b & 1) col = SINGLE[themeKey()];
    } else {
      for (let b = 0; b < PERIODS.length; b++) {
        if ((v >> b & 1) && state.enabled.has(PERIODS[b])) { col = ramp[b]; break; }
      }
    }
    if (col) { const [r, g, bl] = hexRgb(col); lut.set([r, g, bl, 255], v * 4); }
  }
  LUT = lut;
}

async function emptyPng() {
  if (!EMPTY_PNG) {
    const c = new OffscreenCanvas(1, 1);
    c.getContext('2d').clearRect(0, 0, 1, 1); // convertToBlob needs a context
    EMPTY_PNG =await (await c.convertToBlob({ type: 'image/png' })).arrayBuffer();
  }
  return EMPTY_PNG;
}

async function tileValues(z, x, y, signal) {
  const key = `${z}/${x}/${y}`;
  if (valueCache.has(key)) {
    const v = valueCache.get(key); valueCache.delete(key); valueCache.set(key, v); return v;
  }
  const res = await floodPM.getZxy(z, x, y, signal);
  let vals = null;
  if (res && res.data && res.data.byteLength) {
    const bmp = await createImageBitmap(new Blob([res.data], { type: 'image/png' }),
      { premultiplyAlpha: 'none', colorSpaceConversion: 'none' });
    const c = new OffscreenCanvas(bmp.width, bmp.height);
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bmp, 0, 0);
    const px = ctx.getImageData(0, 0, bmp.width, bmp.height).data;
    vals = new Uint8Array(bmp.width * bmp.height);
    for (let i = 0, j = 0; i < vals.length; i++, j += 4) vals[i] = px[j];
    bmp.close?.();
  }
  valueCache.set(key, vals);
  if (valueCache.size > CACHE_MAX) valueCache.delete(valueCache.keys().next().value);
  return vals;
}

maplibregl.addProtocol('flood', async (params, abort) => {
  const m = params.url.match(/^flood:\/\/v\d+\/(\d+)\/(\d+)\/(\d+)/);
  const [z, x, y] = m.slice(1).map(Number);
  const vals = await tileValues(z, x, y, abort.signal);
  if (!vals) return { data: await emptyPng() };
  const n = Math.round(Math.sqrt(vals.length));
  const img = new ImageData(n, n);
  const out = img.data, lut = LUT;
  let any = false;
  for (let i = 0, j = 0; i < vals.length; i++, j += 4) {
    const v = vals[i];
    if (v) { const k = v * 4; if (lut[k + 3]) { out[j] = lut[k]; out[j + 1] = lut[k + 1]; out[j + 2] = lut[k + 2]; out[j + 3] = 255; any = true; } }
  }
  if (!any) return { data: await emptyPng() };
  const c = new OffscreenCanvas(n, n);
  c.getContext('2d').putImageData(img, 0, 0);
  return { data: await (await c.convertToBlob({ type: 'image/png' })).arrayBuffer() };
});

function refreshFlood() {
  rebuildLut();
  floodVersion++;
  const src = map && map.getSource('flood');
  if (src) src.setTiles([`flood://v${floodVersion}/{z}/{x}/{y}`]);
}

/* ------------------------------------------------------------------ basemap style */
function palette() {
  return isDark() ? {
    bg: '#191a1c', land: '#1d1f22', wood: '#1f2622', grass: '#20251f', ice: '#262a2e', sand: '#26241f',
    water: '#0f2235', waterway: '#1a3a5c', park: '#1e2620', building: '#27292c',
    road: '#3a3d42', roadMajor: '#4a4e55', roadCase: '#191a1c', rail: '#34363a',
    border: '#6b6f78', borderState: '#4d5158', label: '#d6d7da', labelHalo: '#121314', waterLabel: '#7fa7cf',
    stream: '#c9d6e6', streamCase: 'rgba(0,0,0,0.35)', huc: 'rgba(240,239,236,0.35)', hucSel: '#f0efec', hilite: '#f08a5d',
  } : {
    bg: '#f3f1ec', land: '#f3f1ec', wood: '#e3eadc', grass: '#e9eedd', ice: '#f5f8fa', sand: '#efe9d8',
    water: '#bcd3e8', waterway: '#9dbfde', park: '#dfe8d3', building: '#e2ded6',
    road: '#ffffff', roadMajor: '#ffffff', roadCase: '#d7d2c7', rail: '#cfcac0',
    border: '#7e7a72', borderState: '#a8a398', label: '#3d3b37', labelHalo: '#ffffff', waterLabel: '#3f6a94',
    stream: '#24476b', streamCase: 'rgba(255,255,255,0.6)', huc: 'rgba(40,40,40,0.35)', hucSel: '#0b0b0b', hilite: '#eb6834',
  };
}

const NAME = ['coalesce', ['get', 'name:en'], ['get', 'name_en'], ['get', 'name']];
const FONT = ['Noto Sans Regular'];
const FONT_B = ['Noto Sans Bold'];
const FONT_I = ['Noto Sans Italic'];

function buildStyle() {
  const p = palette();
  const sat = state.base === 'satellite', relief = state.base === 'relief';
  const vis = (on) => (on ? 'visible' : 'none');
  const vec = !sat; // vector land/water fills hidden over imagery
  const order = ['interpolate', ['linear'], ['get', 'o'], 1, 0.5, 4, 1.1, 7, 2.2, 10, 3.6];
  // zoom must be the top-level input, so each extra width (casing, highlight) is added per stop
  const sw = (add = 0) => ['interpolate', ['exponential', 1.6], ['zoom'],
    4, ['+', ['*', order, 0.45], add], 8, ['+', ['*', order, 0.8], add],
    12, ['+', ['*', order, 1.4], add], 15, ['+', ['*', order, 2.4], add]];
  const streamWidth = sw();
  const coverageColor = ['match', ['get', 's'],
    'mapped', STATUS.mapped.color, 'dry', STATUS.dry.color, 'notrun', STATUS.notrun.color, STATUS.nohand.color];
  const sel = state.selectedHuc || '';

  return {
    version: 8,
    projection: { type: state.proj },
    sky: isDark()
      ? { 'sky-color': '#0b1220', 'horizon-color': '#1c2a3a', 'fog-color': '#121a24', 'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 1, 5, 1, 7, 0] }
      : { 'sky-color': '#bcd7f2', 'horizon-color': '#e9f1fa', 'fog-color': '#f3f6fa', 'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 1, 5, 1, 7, 0] },
    glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
    sources: {
      omt: { type: 'vector', url: 'https://tiles.openfreemap.org/planet',
        attribution: '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> © <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>' },
      imagery: { type: 'raster', tileSize: 256, maxzoom: 16,
        tiles: ['https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}'],
        attribution: 'Imagery: USGS The National Map' },
      relief: { type: 'raster', tileSize: 256, maxzoom: 13,
        tiles: ['https://basemap.nationalmap.gov/arcgis/rest/services/USGSShadedReliefOnly/MapServer/tile/{z}/{y}/{x}'],
        attribution: 'Relief: USGS The National Map' },
      flood: { type: 'raster', tileSize: 256, minzoom: 0, maxzoom: 14, tiles: [`flood://v${floodVersion}/{z}/{x}/{y}`],
        bounds: [-125.5, 24, -66.5, 49.6],
        attribution: 'Flood extents: FIMserv / NOAA OWP HAND + NWM' },
      huc8: { type: 'vector', url: 'pmtiles://' + new URL('data/huc8.pmtiles', CONFIG.site).href, promoteId: 'h',
        attribution: 'Watersheds: USGS WBD' },
      streams: { type: 'vector', url: 'pmtiles://' + CONFIG.dataBase + 'streams.pmtiles', promoteId: 'id',
        attribution: 'Streams: NOAA NWM, NHDPlus V2' },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': p.bg } },
      { id: 'imagery', type: 'raster', source: 'imagery', layout: { visibility: vis(sat) } },
      { id: 'landcover', type: 'fill', source: 'omt', 'source-layer': 'landcover', layout: { visibility: vis(vec) },
        paint: { 'fill-color': ['match', ['get', 'class'], 'wood', p.wood, 'forest', p.wood, 'grass', p.grass, 'ice', p.ice, 'sand', p.sand, p.grass],
          'fill-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.5, 10, 0.8] } },
      { id: 'park', type: 'fill', source: 'omt', 'source-layer': 'park', layout: { visibility: vis(vec) }, paint: { 'fill-color': p.park, 'fill-opacity': 0.6 } },
      { id: 'relief', type: 'raster', source: 'relief', layout: { visibility: vis(relief) },
        paint: { 'raster-opacity': isDark() ? 0.35 : 0.55, 'raster-saturation': -1, 'raster-contrast': isDark() ? -0.2 : 0 } },
      { id: 'water', type: 'fill', source: 'omt', 'source-layer': 'water', layout: { visibility: vis(vec) }, paint: { 'fill-color': p.water } },
      { id: 'waterway', type: 'line', source: 'omt', 'source-layer': 'waterway', minzoom: 8, layout: { visibility: vis(vec && !state.streams) },
        paint: { 'line-color': p.waterway, 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.5, 14, 2] } },
      { id: 'building', type: 'fill', source: 'omt', 'source-layer': 'building', minzoom: 14, layout: { visibility: vis(vec) }, paint: { 'fill-color': p.building } },
      { id: 'road-case', type: 'line', source: 'omt', 'source-layer': 'transportation', minzoom: 7,
        filter: ['in', ['get', 'class'], ['literal', ['motorway', 'trunk', 'primary', 'secondary']]],
        layout: { 'line-cap': 'round', 'line-join': 'round', visibility: vis(vec) },
        paint: { 'line-color': p.roadCase, 'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 7, 1, 12, 4, 16, 14] } },
      { id: 'road-minor', type: 'line', source: 'omt', 'source-layer': 'transportation', minzoom: 12,
        filter: ['in', ['get', 'class'], ['literal', ['tertiary', 'minor', 'service']]],
        layout: { 'line-cap': 'round', 'line-join': 'round', visibility: vis(vec) },
        paint: { 'line-color': p.road, 'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 12, 0.6, 16, 6] } },
      { id: 'road', type: 'line', source: 'omt', 'source-layer': 'transportation', minzoom: 5,
        filter: ['in', ['get', 'class'], ['literal', ['motorway', 'trunk', 'primary', 'secondary']]],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': sat ? 'rgba(255,255,255,0.45)' : p.roadMajor,
          'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 5, 0.5, 12, 2.5, 16, 11] } },
      { id: 'coverage', type: 'fill', source: 'huc8', 'source-layer': 'huc8', layout: { visibility: vis(state.coverage) },
        paint: { 'fill-color': coverageColor, 'fill-opacity': 0.35 } },
      { id: 'flood', type: 'raster', source: 'flood',
        paint: { 'raster-opacity': state.opacity, 'raster-resampling': 'nearest', 'raster-fade-duration': 0 } },
      { id: 'streams-case', type: 'line', source: 'streams', 'source-layer': 'streams', layout: { visibility: vis(state.streams), 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': p.streamCase, 'line-width': sw(1.5) } },
      { id: 'streams', type: 'line', source: 'streams', 'source-layer': 'streams', layout: { visibility: vis(state.streams), 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': sat ? '#d9ecff' : p.stream, 'line-width': streamWidth, 'line-opacity': 0.9 } },
      { id: 'streams-named', type: 'line', source: 'streams', 'source-layer': 'streams',
        filter: ['==', ['get', 'n'], state.highlightName || '\u0000'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': p.hilite, 'line-width': sw(2.5) } },
      { id: 'streams-sel', type: 'line', source: 'streams', 'source-layer': 'streams',
        filter: ['==', ['get', 'id'], state.selectedReach ? state.selectedReach.id : -1],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': p.hilite, 'line-width': sw(4) } },
      { id: 'huc8-line', type: 'line', source: 'huc8', 'source-layer': 'huc8', layout: { visibility: vis(state.huc8) },
        paint: { 'line-color': sat ? 'rgba(255,255,255,0.55)' : p.huc,
          'line-width': ['interpolate', ['linear'], ['zoom'],
            3, ['case', ['boolean', ['feature-state', 'hover'], false], 2.2, 0.3],
            8, ['case', ['boolean', ['feature-state', 'hover'], false], 2.4, 0.9],
            12, ['case', ['boolean', ['feature-state', 'hover'], false], 2.8, 1.4]] } },
      { id: 'huc8-hit', type: 'fill', source: 'huc8', 'source-layer': 'huc8', paint: { 'fill-color': '#000', 'fill-opacity': 0 } },
      { id: 'huc8-sel', type: 'line', source: 'huc8', 'source-layer': 'huc8', filter: ['==', ['get', 'h'], sel],
        paint: { 'line-color': sat ? '#ffffff' : p.hucSel, 'line-width': 2.5 } },
      { id: 'boundary-state', type: 'line', source: 'omt', 'source-layer': 'boundary',
        filter: ['all', ['==', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]],
        paint: { 'line-color': sat ? 'rgba(255,255,255,0.6)' : p.borderState, 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.6, 10, 1.4], 'line-dasharray': [3, 2] } },
      { id: 'boundary-country', type: 'line', source: 'omt', 'source-layer': 'boundary',
        filter: ['all', ['==', ['get', 'admin_level'], 2], ['!=', ['get', 'maritime'], 1]],
        paint: { 'line-color': sat ? 'rgba(255,255,255,0.8)' : p.border, 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 0.6, 8, 2] } },
      { id: 'water-label', type: 'symbol', source: 'omt', 'source-layer': 'water_name', minzoom: 4,
        filter: ['==', ['geometry-type'], 'Point'],
        layout: { 'text-field': NAME, 'text-font': FONT_I, 'text-size': 12 },
        paint: { 'text-color': sat ? '#d9ecff' : p.waterLabel, 'text-halo-color': sat ? 'rgba(0,0,0,0.6)' : p.labelHalo, 'text-halo-width': 1.2 } },
      { id: 'water-label-line', type: 'symbol', source: 'omt', 'source-layer': 'water_name', minzoom: 4,
        filter: ['==', ['geometry-type'], 'LineString'],
        layout: { 'text-field': NAME, 'text-font': FONT_I, 'text-size': 12, 'symbol-placement': 'line' },
        paint: { 'text-color': sat ? '#d9ecff' : p.waterLabel, 'text-halo-color': sat ? 'rgba(0,0,0,0.6)' : p.labelHalo, 'text-halo-width': 1.2 } },
      { id: 'stream-label', type: 'symbol', source: 'streams', 'source-layer': 'streams', minzoom: 9,
        filter: ['!=', ['get', 'n'], ''],
        layout: { visibility: vis(state.streams), 'text-field': ['get', 'n'], 'text-font': FONT_I, 'text-size': 11, 'symbol-placement': 'line', 'symbol-spacing': 400 },
        paint: { 'text-color': sat ? '#d9ecff' : p.waterLabel, 'text-halo-color': sat ? 'rgba(0,0,0,0.6)' : p.labelHalo, 'text-halo-width': 1.2 } },
      { id: 'road-label', type: 'symbol', source: 'omt', 'source-layer': 'transportation_name', minzoom: 12,
        layout: { 'text-field': NAME, 'text-font': FONT, 'text-size': 10, 'symbol-placement': 'line' },
        paint: { 'text-color': p.label, 'text-halo-color': p.labelHalo, 'text-halo-width': 1.2 } },
      { id: 'place-small', type: 'symbol', source: 'omt', 'source-layer': 'place', minzoom: 8,
        filter: ['in', ['get', 'class'], ['literal', ['town', 'village']]],
        layout: { 'text-field': NAME, 'text-font': FONT, 'text-size': 11 },
        paint: { 'text-color': sat ? '#fff' : p.label, 'text-halo-color': sat ? 'rgba(0,0,0,0.7)' : p.labelHalo, 'text-halo-width': 1.4 } },
      { id: 'place-city', type: 'symbol', source: 'omt', 'source-layer': 'place', minzoom: 4,
        filter: ['==', ['get', 'class'], 'city'],
        layout: { 'text-field': NAME, 'text-font': FONT_B, 'text-size': ['interpolate', ['linear'], ['zoom'], 4, 11, 10, 15] },
        paint: { 'text-color': sat ? '#fff' : p.label, 'text-halo-color': sat ? 'rgba(0,0,0,0.7)' : p.labelHalo, 'text-halo-width': 1.5 } },
      { id: 'place-state', type: 'symbol', source: 'omt', 'source-layer': 'place', minzoom: 3, maxzoom: 7,
        filter: ['==', ['get', 'class'], 'state'],
        layout: { 'text-field': NAME, 'text-font': FONT, 'text-size': 11, 'text-transform': 'uppercase', 'text-letter-spacing': 0.12 },
        paint: { 'text-color': sat ? '#fff' : p.label, 'text-opacity': 0.6, 'text-halo-color': sat ? 'rgba(0,0,0,0.7)' : p.labelHalo, 'text-halo-width': 1.2 } },
    ],
  };
}

/* ------------------------------------------------------------------ map */
let map;
function initMap() {
  rebuildLut();
  map = new maplibregl.Map({
    container: 'map',
    style: buildStyle(),
    center: [-97.5, 39], zoom: 3.4, minZoom: 1.5, maxZoom: 17,
    hash: 'map',
    attributionControl: { compact: true },
    canvasContextAttributes: { preserveDrawingBuffer: true },
    preserveDrawingBuffer: true,
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'imperial' }), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
  map.addControl(new maplibregl.GeolocateControl({ fitBoundsOptions: { maxZoom: 11 } }), 'top-right');

  let hovered = null;
  const hoverPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
  map.on('mousemove', (e) => {
    const reach = state.streams && map.getZoom() >= 6 && pickReach(e.point);
    const huc = map.queryRenderedFeatures(e.point, { layers: ['huc8-hit'] })[0];
    const h = huc && huc.properties.h;
    if (hovered !== h) {
      if (hovered) map.setFeatureState({ source: 'huc8', sourceLayer: 'huc8', id: hovered }, { hover: false });
      hovered = h || null;
      if (hovered) map.setFeatureState({ source: 'huc8', sourceLayer: 'huc8', id: hovered }, { hover: true });
    }
    map.getCanvas().style.cursor = reach || huc ? 'pointer' : '';
    if (reach) {
      hoverPopup.setLngLat(e.lngLat).setHTML(`<strong>${esc(reach.properties.n || 'Unnamed reach')}</strong><br>100-yr ${fmtSig(reach.properties.q100)} m³/s`).addTo(map);
    } else if (huc && map.getZoom() < 9) {
      const c = BY_HUC.get(h);
      hoverPopup.setLngLat(e.lngLat).setHTML(`<strong>${esc(c ? c.n : h)}</strong><br>HUC8 ${esc(h)} · ${esc(STATUS[c?.s]?.label || '')}`).addTo(map);
    } else hoverPopup.remove();
  });
  map.on('mouseout', () => hoverPopup.remove());
  const clickPopup = new maplibregl.Popup({ closeButton: true, offset: 6, maxWidth: '260px' });
  map.on('click', async (e) => {
    const reach = state.streams && pickReach(e.point);
    if (reach) { selectReach(reach); return; }
    const huc = map.queryRenderedFeatures(e.point, { layers: ['huc8-hit'] })[0];
    if (huc) selectHuc(huc.properties.h, { fly: false });
    const at = await floodAt(e.lngLat);
    if (at && at.periods.length) {
      clickPopup.setLngLat(e.lngLat).setHTML(`<strong>Floods at the ${at.periods.map((p) => p + '-').join(', ').replace(/-$/, '')}-year flood${at.periods.length > 1 ? 's' : ''}</strong>`
        + (at.z < 14 ? `<br><span class="sub">Zoomed out: somewhere in this ${fmt(at.cell)} m cell. Zoom in for the exact spot.</span>` : '')).addTo(map);
    } else clickPopup.remove();
  });
  map.on('error', (e) => {
    const msg = String(e?.error?.message || e?.error || '');
    console.warn('map error:', msg);
    if (/flood\.pmtiles|streams\.pmtiles/.test(msg) && !initMap._warned) {
      initMap._warned = true; toast('Some map data could not be loaded. Try reloading the page.');
    }
  });
}

// Which return periods flood the pixel under a point (the tile set's own bit mask).
async function floodAt(lngLat) {
  const z = Math.max(0, Math.min(14, Math.round(map.getZoom())));
  const n = 256 * 2 ** z;
  const sin = Math.sin(lngLat.lat * Math.PI / 180);
  const px = (lngLat.lng + 180) / 360 * n;
  const py = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * n;
  const tx = Math.floor(px / 256), ty = Math.floor(py / 256);
  const vals = await tileValues(z, tx, ty).catch(() => null);
  const cell = 2 * Math.PI * 6378137 * Math.cos(lngLat.lat * Math.PI / 180) / n;
  if (!vals) return { z, cell, periods: [] };
  const v = vals[(Math.floor(py) - ty * 256) * 256 + (Math.floor(px) - tx * 256)];
  return { z, cell, periods: PERIODS.filter((p, i) => v >> i & 1) };
}

function pickReach(point) {
  const box = [[point.x - 5, point.y - 5], [point.x + 5, point.y + 5]];
  const feats = map.queryRenderedFeatures(box, { layers: ['streams'] });
  if (!feats.length) return null;
  feats.sort((a, b) => (b.properties.o || 0) - (a.properties.o || 0));
  return feats[0];
}

function restyle() {
  if (!map) return;
  map.setStyle(buildStyle(), { diff: true });
  document.body.classList.toggle('dark-map', isDark());
}

/* ------------------------------------------------------------------ legend & controls */
function renderLegend() {
  const ramp = RAMP[themeKey()];
  const has500 = STATS ? STATS.periods['500'].hucs : 0;
  $('#legend').innerHTML = PERIODS.map((p, i) => {
    const single = state.mode === 'single';
    const on = single ? state.single === p : state.enabled.has(p);
    const color = single ? SINGLE[themeKey()] : ramp[i];
    const input = single
      ? `<input type="radio" name="period" value="${p}" ${on ? 'checked' : ''}>`
      : `<input type="checkbox" value="${p}" ${on ? 'checked' : ''}>`;
    const extra = p === 500 ? ` <span class="sub">${fmt(has500)} watersheds</span>` : '';
    return `<li class="${on ? '' : 'off'}"><label>${input}<span class="sw" style="background:${color}"></span>
      <span class="lbl">${p}-year</span>${extra}<span class="chance">${CHANCE[p]}/yr</span></label></li>`;
  }).join('');
  $('#mode-hint').textContent = state.mode === 'single'
    ? 'Everything flooded by the chosen return period.'
    : 'Each place is colored by the most frequent flood that reaches it.';
  $$('#legend input').forEach((el) => el.addEventListener('change', () => {
    const p = Number(el.value);
    if (state.mode === 'single') state.single = p;
    else if (el.checked) state.enabled.add(p); else state.enabled.delete(p);
    saveState(); renderLegend(); refreshFlood();
  }));
  $('#note-500').textContent = has500
    ? `500-year extents exist only where the LP-III method was used (${fmt(has500)} watersheds). Elsewhere the 100-year flood is the largest.`
    : '';
  $('#coverage-legend').innerHTML = Object.entries(STATUS).map(([k, s]) =>
    `<li><span class="sw" style="background:${s.color};opacity:.7"></span>${s.label}<span class="chance">${fmt(STATS?.coverage.status[k] || 0)}</span></li>`).join('');
}

function setSeg(attr, val) {
  $$(`[data-${attr}]`).forEach((b) => b.setAttribute('aria-checked', String(b.dataset[attr] === val)));
}

function bindControls() {
  $$('[data-mode]').forEach((b) => b.addEventListener('click', () => {
    state.mode = b.dataset.mode; setSeg('mode', state.mode); saveState(); renderLegend(); refreshFlood();
  }));
  $$('[data-base]').forEach((b) => b.addEventListener('click', () => {
    state.base = b.dataset.base; setSeg('base', state.base); saveState(); restyle();
  }));
  $$('[data-proj]').forEach((b) => b.addEventListener('click', () => {
    state.proj = b.dataset.proj; setSeg('proj', state.proj); saveState();
    map.setProjection({ type: state.proj });
  }));
  $('#opacity').addEventListener('input', (e) => {
    state.opacity = Number(e.target.value); map.setPaintProperty('flood', 'raster-opacity', state.opacity); saveState();
  });
  const layerToggle = (id, key) => $(id).addEventListener('change', (e) => {
    state[key] = e.target.checked; saveState(); restyle();
    if (key === 'coverage') $('#coverage-legend').hidden = !state.coverage;
  });
  layerToggle('#lyr-streams', 'streams');
  layerToggle('#lyr-huc8', 'huc8');
  layerToggle('#lyr-coverage', 'coverage');

  $('#btn-theme').addEventListener('click', () => {
    state.theme = { auto: 'light', light: 'dark', dark: 'auto' }[state.theme];
    applyTheme(); saveState(); renderLegend(); refreshFlood(); restyle();
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (state.theme === 'auto') { renderLegend(); refreshFlood(); restyle(); }
  });
  $('#btn-panel').addEventListener('click', () => $('#panel-left').classList.toggle('open'));
  $('#close-right').addEventListener('click', closeDetails);
  $('#btn-stats').addEventListener('click', openStats);
  $('#btn-about').addEventListener('click', () => $('#dlg-about').showModal());
  $$('[data-open="about"]').forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); $('#dlg-about').showModal(); }));
  $$('dialog [data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));
  $$('dialog').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));

  const menu = $('#export-menu'), btn = $('#btn-export');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden));
    $('[data-export="huc"]').disabled = !(state.selectedHuc && BY_HUC.get(state.selectedHuc)?.s === 'mapped');
  });
  document.addEventListener('click', () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
  $$('[data-export]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.export;
    if (k === 'png') exportPng();
    if (k === 'huc') selectHuc(state.selectedHuc, { fly: false, scrollTo: 'downloads' });
    if (k === 'stats') downloadStatsCsv();
    if (k === 'catalog') downloadCatalogCsv();
  }));
}

function applyTheme() {
  if (state.theme === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', state.theme);
  $('#btn-theme').title = `Color theme: ${state.theme === 'auto' ? 'automatic' : state.theme}`;
}

function saveState() {
  store.set('state', { mode: state.mode, enabled: [...state.enabled], single: state.single, opacity: state.opacity,
    base: state.base, proj: state.proj, theme: state.theme, streams: state.streams, huc8: state.huc8, coverage: state.coverage });
}
function loadState() {
  const s = store.get('state', null);
  if (!s) return;
  Object.assign(state, s, { enabled: new Set(s.enabled || PERIODS) });
}
function syncControls() {
  setSeg('mode', state.mode); setSeg('base', state.base); setSeg('proj', state.proj);
  $('#opacity').value = state.opacity;
  $('#lyr-streams').checked = state.streams; $('#lyr-huc8').checked = state.huc8; $('#lyr-coverage').checked = state.coverage;
  $('#coverage-legend').hidden = !state.coverage;
}

/* ------------------------------------------------------------------ HUC8 list */
function stateList() {
  const names = new Set();
  for (const c of CATALOG) if (c.ps) names.add(c.ps);
  return [...names].sort();
}
function renderHucList() {
  const st = $('#f-state').value, status = $('#f-status').value, q = norm($('#f-text').value || '');
  const rows = CATALOG.filter((c) => (!st || c.ps === st || (c.st || '').split(',').includes(STATE_ABBR[st]))
    && (!status || c.s === status) && (!q || c.h.startsWith(q) || norm(c.n).includes(q)));
  $('#huc-count').textContent = `${fmt(rows.length)} of ${fmt(CATALOG.length)}`;
  const shown = rows.slice(0, 400);
  $('#huc-list').innerHTML = shown.map((c) => `<li><button data-h="${c.h}"><span class="dot" style="background:${STATUS[c.s].color}"></span>
    <span class="nm">${esc(c.n)}</span><span class="code">${c.h}</span></button></li>`).join('')
    + (rows.length > shown.length ? `<li class="sub" style="padding:6px 4px">${fmt(rows.length - shown.length)} more; narrow the filter</li>` : '');
}
const STATE_ABBR = {
  Alabama: 'AL', Alaska: 'AK', 'American Samoa': 'AS', Arizona: 'AZ', Arkansas: 'AR', California: 'CA', Colorado: 'CO', Connecticut: 'CT',
  Delaware: 'DE', 'District Of Columbia': 'DC', Florida: 'FL', Georgia: 'GA', Guam: 'GU', Hawaii: 'HI', Idaho: 'ID', Illinois: 'IL', Indiana: 'IN',
  Iowa: 'IA', Kansas: 'KS', Kentucky: 'KY', Louisiana: 'LA', Maine: 'ME', Maryland: 'MD', Massachusetts: 'MA', Michigan: 'MI', Minnesota: 'MN',
  Mississippi: 'MS', Missouri: 'MO', Montana: 'MT', Nebraska: 'NE', Nevada: 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM',
  'New York': 'NY', 'North Carolina': 'NC', 'North Dakota': 'ND', Ohio: 'OH', Oklahoma: 'OK', Oregon: 'OR', Pennsylvania: 'PA', 'Puerto Rico': 'PR',
  'Rhode Island': 'RI', 'South Carolina': 'SC', 'South Dakota': 'SD', Tennessee: 'TN', Texas: 'TX', 'US Virgin Islands': 'VI', Utah: 'UT',
  Vermont: 'VT', Virginia: 'VA', Washington: 'WA', 'West Virginia': 'WV', Wisconsin: 'WI', Wyoming: 'WY', 'Northern Mariana Islands': 'MP',
};
function bindHucList() {
  const sel = $('#f-state');
  const names = Object.keys(STATE_ABBR).sort();
  sel.innerHTML = '<option value="">All states</option>' + names.map((n) => `<option>${esc(n)}</option>`).join('');
  ['#f-state', '#f-status'].forEach((id) => $(id).addEventListener('change', renderHucList));
  $('#f-text').addEventListener('input', debounce(renderHucList, 120));
  $('#huc-list').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-h]');
    if (b) { selectHuc(b.dataset.h); $('#panel-left').classList.remove('open'); }
  });
  renderHucList();
}

/* ------------------------------------------------------------------ details panels */
function openDetails(html) {
  $('#details').innerHTML = html;
  $('#panel-right').hidden = false;
}
function closeDetails() {
  $('#panel-right').hidden = true;
  state.selectedHuc = null; state.selectedReach = null;
  setHashParam('huc', null);
  if (map) { map.setFilter('huc8-sel', ['==', ['get', 'h'], '']); map.setFilter('streams-sel', ['==', ['get', 'id'], -1]); }
}

function hucBounds(c) {
  return c.b ? [[c.b[0], c.b[1]], [c.b[2], c.b[3]]] : null;
}

async function selectHuc(h, opts = {}) {
  const c = BY_HUC.get(h);
  if (!c) return;
  state.selectedHuc = h; state.selectedReach = null;
  setHashParam('huc', h);
  map.setFilter('huc8-sel', ['==', ['get', 'h'], h]);
  map.setFilter('streams-sel', ['==', ['get', 'id'], -1]);
  if (opts.fly !== false) {
    const b = hucBounds(c);
    if (b) map.fitBounds(b, { padding: { top: 40, bottom: 40, left: window.innerWidth > 760 ? 360 : 20, right: window.innerWidth > 760 ? 400 : 20 }, maxZoom: 11 });
    else {
      const f = map.querySourceFeatures('huc8', { sourceLayer: 'huc8', filter: ['==', ['get', 'h'], h] })[0];
      if (f) map.fitBounds(bboxOf(f.geometry), { padding: 60, maxZoom: 10 });
    }
  }
  openDetails(hucHtml(c));
  bindChartTips($('#details'));
  $('#details [data-meta]')?.addEventListener('click', () => downloadBlob(`${h}_metadata.json`, JSON.stringify(hucMetadata(c), null, 2), 'application/json'));
  if (opts.scrollTo) $('#details #downloads')?.scrollIntoView({ behavior: 'smooth' });
}

function bboxOf(geom) {
  let minx = 180, miny = 90, maxx = -180, maxy = -90;
  const walk = (a) => { if (typeof a[0] === 'number') { minx = Math.min(minx, a[0]); maxx = Math.max(maxx, a[0]); miny = Math.min(miny, a[1]); maxy = Math.max(maxy, a[1]); } else a.forEach(walk); };
  walk(geom.coordinates);
  return [[minx, miny], [maxx, maxy]];
}

function hucHtml(c) {
  const st = STATUS[c.s];
  const head = `<div class="kicker">Watershed · HUC8 ${c.h}</div><h2>${esc(c.n)}</h2>
    <p class="subtitle">${esc([c.h6, c.h4, c.h2].filter(Boolean).join(' · '))}</p>
    <span class="badge"><span class="dot" style="background:${st.color}"></span>${st.label}</span>`;
  const kvBase = [['States', c.st], ['Area', `${fmt(c.a)} km²`]];
  if (c.s !== 'mapped') {
    const why = {
      dry: 'This watershed was run, but no return-period discharge was available for its reaches (the CIROH flows cover the lower 48 only), so no flood extent was produced.',
      notrun: 'NOAA publishes HAND data for this watershed, but it was not run: it lies outside the US.',
      nohand: 'NOAA does not publish HAND data for this watershed (open water, or outside the HAND coverage), so it cannot be mapped with this method.',
    }[c.s];
    return head + kv([...kvBase, ...(c.m ? [['Method', c.m], ['HAND release', c.hv]] : [])]) + `<p class="msg">${why}</p>`;
  }
  const periods = c.p;
  const bars = periods.map((p) => ({ k: `${p}-yr`, v: c.w[String(p)] ?? 0, tip: `${p}-year: ${fmt(c.w[String(p)], 1)} km²` }));
  const covTxt = c.cov == null ? '' : `${fmt(c.cov, 1)}% of reaches`;
  const rows = [
    ...kvBase,
    ['Method', c.m === 'CIROH' ? 'CIROH return-period flows' : c.m === 'LP-III' ? 'LP-III fit, NWM retrospective' : c.m],
    ['Periods', periods.map((p) => p + '').join(', ') + ' yr'],
    ...(c.cov != null ? [['CIROH coverage', covTxt]] : []),
    ['HAND release', c.hv || '–'],
    ['Mapped on', c.d || '–'],
    ['Reaches', fmt(c.r)], ['Stream length', `${fmt(c.km)} km`], ['Named reaches', fmt(c.nm)], ['USGS gages', fmt(c.g)],
    ...(c.nb ? [['Nesting breaks', `${fmt(c.nb)} px <span class="sub" title="Pixels wet at a shorter return period but dry at a longer one">?</span>`]] : []),
  ];
  const base = CONFIG.dataBase;
  return head + kv(rows)
    + `<div class="section-title">Flooded area by return period</div>` + barChart(bars, 'km²')
    + table(['Return period', 'Annual chance', 'Flooded km²'], periods.map((p) => [`${p}-year`, CHANCE[p], fmt(c.w[String(p)], 2)]))
    + `<div class="section-title" id="downloads">Downloads</div>
      <div class="downloads">
        <a href="${base}cogs/${c.h}.tif" download>Flood extent GeoTIFF<small>bit mask, all periods</small></a>
        <a href="${base}reaches/${c.h}.csv" download>Reach table (CSV)<small>${fmt(c.r)} reaches</small></a>
        <button data-meta>Metadata (JSON)<small>this watershed</small></button>
      </div>`;
}

function kv(rows) {
  return `<dl class="kv">${rows.filter((r) => r[1] != null && r[1] !== '').map(([k, v]) => `<dt>${esc(k)}</dt><dd>${typeof v === 'string' && v.includes('<span') ? v : esc(v)}</dd>`).join('')}</dl>`;
}
function table(head, rows, cls = '') {
  return `<table class="tbl ${cls}"><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((v) => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function hucMetadata(c) {
  return {
    huc8: c.h, name: c.n, states: c.st, area_km2: c.a, huc2: c.h2, huc4: c.h4, huc6: c.h6, status: STATUS[c.s].label,
    method: c.m, return_periods_yr: c.p, flooded_km2: c.w, hand_release: c.hv, ciroh_reach_coverage_pct: c.cov,
    mapped_on: c.d, reaches: c.r, stream_km: c.km, named_reaches: c.nm, usgs_gages: c.g, nesting_break_pixels: c.nb,
    bounds_wgs84: c.b, files: { flood_geotiff: `${CONFIG.dataBase}cogs/${c.h}.tif`, reaches_csv: `${CONFIG.dataBase}reaches/${c.h}.csv` },
    dataset: META ? { title: META.title, version: META.version, license: META.license, citation: META.citation } : undefined,
  };
}

function selectReach(f) {
  const p = f.properties;
  state.selectedReach = p; state.selectedHuc = p.h;
  map.setFilter('streams-sel', ['==', ['get', 'id'], p.id]);
  map.setFilter('huc8-sel', ['==', ['get', 'h'], p.h]);
  const c = BY_HUC.get(p.h);
  const periods = PERIODS.filter((t) => p['q' + t] != null);
  const pts = periods.map((t) => ({ x: t, y: p['q' + t] }));
  const gage = p.g ? `<a href="https://waterdata.usgs.gov/monitoring-location/USGS-${encodeURIComponent(p.g)}/" target="_blank" rel="noopener">${esc(p.g)}</a>` : null;
  const rows = [
    ['NWM feature ID', String(p.id)],
    ['Watershed', c ? `${c.n} (${p.h})` : p.h],
    ['Stream order', p.o], ['Length', `${fmt(p.L, 2)} km`],
    ['Slope', p.s != null ? `${fmt(p.s * 100, 2)}%` : null],
    ['Drainage area', p.da != null ? `${fmt(p.da, 0)} km²` : null],
    ['Lake reach', p.lk ? 'yes' : null],
  ];
  const html = `<div class="kicker">Stream reach</div><h2>${esc(p.n || 'Unnamed reach')}</h2>
    <p class="subtitle">${c ? esc(c.m) + ' method' : ''}</p>`
    + kv(rows) + (gage ? `<dl class="kv"><dt>USGS gage</dt><dd>${gage}</dd></dl>` : '')
    + `<div class="section-title">Peak discharge by return period</div>` + lineChart(pts, 'm³/s')
    + table(['Return period', 'Discharge m³/s', 'ft³/s', 'Flooded km²'],
      periods.map((t) => [`${t}-year`, fmtSig(p['q' + t]), fmt(p['q' + t] * 35.3147, 0), p['a' + t] != null ? fmt(p['a' + t], 3) : '–']))
    + `<p class="note">Flooded area is counted inside this reach's own NWM catchment.</p>
      <div class="actions-row"><button class="btn" data-goto-huc>Open watershed</button>
      ${p.n ? `<button class="btn" data-hl-name>Highlight “${esc(p.n)}”</button>` : ''}</div>`;
  openDetails(html);
  bindChartTips($('#details'));
  $('#details [data-goto-huc]').addEventListener('click', () => selectHuc(p.h));
  $('#details [data-hl-name]')?.addEventListener('click', () => highlightName(p.n));
}

function highlightName(name) {
  state.highlightName = name;
  map.setFilter('streams-named', ['==', ['get', 'n'], name || '\u0000']);
}

/* ------------------------------------------------------------------ small SVG charts */
function barChart(items, unit) {
  const W = 320, H = 150, L = 44, B = 22, T = 8;
  const max = Math.max(...items.map((d) => d.v), 0) || 1;
  const nice = niceMax(max);
  const bw = (W - L) / items.length;
  const y = (v) => T + (H - T - B) * (1 - v / nice);
  const ticks = [0, nice / 2, nice];
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Flooded area by return period, ${unit}">
    <g class="grid">${ticks.map((t) => `<line x1="${L}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/>`).join('')}</g>
    <g class="axis">${ticks.map((t) => `<text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${fmtSig(t)}</text>`).join('')}
      ${items.map((d, i) => `<text x="${L + bw * i + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(d.k)}</text>`).join('')}</g>
    ${items.map((d, i) => {
      const x = L + bw * i + bw * 0.18, w = bw * 0.64, top = y(d.v), h = Math.max(0, H - B - top);
      const r = Math.min(4, w / 2, h);
      const path = h > 0 ? `M${x},${H - B} V${top + r} Q${x},${top} ${x + r},${top} H${x + w - r} Q${x + w},${top} ${x + w},${top + r} V${H - B} Z` : '';
      return `<path class="bar" d="${path}"/><rect class="hit" x="${L + bw * i}" y="${T}" width="${bw}" height="${H - T - B}" data-tip="${esc(d.tip)}" data-i="${i}"/>`;
    }).join('')}
  </svg></div>`;
}

function lineChart(pts, unit) {
  if (!pts.length) return '<p class="msg">No discharge values.</p>';
  const W = 320, H = 160, L = 44, B = 24, T = 10, R = 10;
  const lx = (t) => Math.log10(t);
  const x0 = lx(pts[0].x), x1 = lx(pts[pts.length - 1].x) || x0 + 1;
  const max = niceMax(Math.max(...pts.map((p) => p.y)) || 1);
  const x = (t) => L + (W - L - R) * (x1 === x0 ? 0.5 : (lx(t) - x0) / (x1 - x0));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const ticks = [0, max / 2, max];
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.x)},${y(p.y)}`).join(' ');
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Discharge by return period, ${unit}">
    <g class="grid">${ticks.map((t) => `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`).join('')}</g>
    <g class="axis">${ticks.map((t) => `<text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${fmtSig(t)}</text>`).join('')}
      ${pts.map((p) => `<text x="${x(p.x)}" y="${H - 6}" text-anchor="middle">${p.x}</text>`).join('')}
      <text x="${W - R}" y="${T}" text-anchor="end">${unit}</text></g>
    <path class="line" d="${d}"/>
    ${pts.map((p) => `<circle class="pt" cx="${x(p.x)}" cy="${y(p.y)}" r="4"/>`).join('')}
    ${pts.map((p) => `<circle class="hit" cx="${x(p.x)}" cy="${y(p.y)}" r="14" data-tip="${p.x}-year: ${esc(fmtSig(p.y))} ${unit}"/>`).join('')}
  </svg></div>`;
}

function niceMax(v) {
  const e = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * e >= v) return m * e;
  return 10 * e;
}

function bindChartTips(root) {
  $$('.chart', root).forEach((ch) => {
    let tip = null;
    ch.addEventListener('mousemove', (e) => {
      const t = e.target.closest('[data-tip]');
      $$('.bar', ch).forEach((b) => b.classList.remove('hot'));
      if (!t) { tip?.remove(); tip = null; return; }
      if (t.dataset.i != null) $$('.bar', ch)[Number(t.dataset.i)]?.classList.add('hot');
      if (!tip) { tip = document.createElement('div'); tip.className = 'tip'; ch.appendChild(tip); }
      tip.textContent = t.dataset.tip;
      const r = ch.getBoundingClientRect(), tr = t.getBoundingClientRect();
      tip.style.left = `${tr.left - r.left + tr.width / 2}px`;
      tip.style.top = `${Math.max(14, tr.top - r.top + (t.tagName === 'circle' ? 10 : 0))}px`;
    });
    ch.addEventListener('mouseleave', () => { tip?.remove(); tip = null; $$('.bar', ch).forEach((b) => b.classList.remove('hot')); });
  });
}

/* ------------------------------------------------------------------ search */
const nameShards = new Map();
async function nameShard(k) {
  if (!nameShards.has(k)) nameShards.set(k, loadJSON(`data/search/n_${k}.json`).catch(() => []));
  return nameShards.get(k);
}
async function idLookup(id) {
  const shard = (BigInt(id) % 256n).toString(16).padStart(2, '0');
  const d = await loadJSON(`data/search/i_${shard}.json`).catch(() => ({}));
  return d[String(id)] || null;
}

let searchItems = [], searchSel = -1;
async function runSearch(q) {
  const box = $('#search-results');
  const nq = norm(q);
  if (!nq) { box.hidden = true; $('#search').setAttribute('aria-expanded', 'false'); return; }
  const items = [];
  if (/^\d{2,8}$/.test(nq)) {
    for (const c of CATALOG) if (c.h.startsWith(nq)) { items.push({ kind: 'huc', c }); if (items.length >= 8) break; }
  }
  if (/^\d{5,}$/.test(nq)) {
    const at = await idLookup(nq);
    if (at) items.unshift({ kind: 'reach', id: nq, at });
  }
  if (!/^\d+$/.test(nq)) {
    for (const c of CATALOG) if (norm(c.n).includes(nq)) { items.push({ kind: 'huc', c }); if (items.length >= 6) break; }
    const k = /^[a-z0-9]/.test(nq) ? nq[0] : '_';
    const rows = await nameShard(k);
    let n = 0;
    for (const r of rows) {
      const nn = norm(r[0]);
      if (nn.startsWith(nq) || (nq.length > 2 && nn.includes(' ' + nq))) {
        items.push({ kind: 'river', r }); if (++n >= 12) break;
      }
    }
  }
  if ($('#search').value.trim() !== q) return; // stale
  searchItems = items; searchSel = -1;
  const hucs = items.filter((i) => i.kind === 'huc'), rivers = items.filter((i) => i.kind === 'river'), reaches = items.filter((i) => i.kind === 'reach');
  const rowHtml = (it, idx) => {
    if (it.kind === 'huc') return `<button class="res" role="option" data-i="${idx}"><span class="dot" style="background:${STATUS[it.c.s].color}"></span><span class="t">${esc(it.c.n)}</span><span class="m">HUC8 ${it.c.h} · ${esc(it.c.st || '')}</span></button>`;
    if (it.kind === 'river') {
      const [name, o, km, , hucs2] = it.r;
      const c = BY_HUC.get(hucs2[0]);
      return `<button class="res" role="option" data-i="${idx}"><span class="t">${esc(name)}</span><span class="m">order ${o} · ${fmt(km)} km · ${esc(c ? c.n : hucs2[0])}${hucs2.length > 1 ? ` +${hucs2.length - 1}` : ''}</span></button>`;
    }
    return `<button class="res" role="option" data-i="${idx}"><span class="t">Reach ${esc(it.id)}</span><span class="m">NWM feature ID</span></button>`;
  };
  let html = '';
  const idx = (it) => items.indexOf(it);
  if (reaches.length) html += '<h4>Reach</h4>' + reaches.map((it) => rowHtml(it, idx(it))).join('');
  if (rivers.length) html += '<h4>Rivers and streams</h4>' + rivers.map((it) => rowHtml(it, idx(it))).join('');
  if (hucs.length) html += '<h4>Watersheds</h4>' + hucs.map((it) => rowHtml(it, idx(it))).join('');
  box.innerHTML = html || '<div class="empty">No matches. Try a river name, a watershed name, or an 8-digit HUC code.</div>';
  box.hidden = false; $('#search').setAttribute('aria-expanded', 'true');
}

function chooseSearch(i) {
  const it = searchItems[i];
  if (!it) return;
  $('#search-results').hidden = true;
  if (it.kind === 'huc') { selectHuc(it.c.h); return; }
  if (it.kind === 'river') {
    const [name, , , , , b] = it.r;
    highlightName(name);
    map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 80, maxZoom: 13 });
    toast(`Highlighting ${name}. Click the river for its discharge.`);
    return;
  }
  if (it.kind === 'reach') {
    map.flyTo({ center: it.at, zoom: 13 });
    map.once('idle', () => {
      const f = map.querySourceFeatures('streams', { sourceLayer: 'streams', filter: ['==', ['get', 'id'], Number(it.id)] })[0];
      if (f) selectReach(f);
    });
  }
}

function bindSearch() {
  const input = $('#search');
  input.addEventListener('input', debounce(() => runSearch(input.value.trim()), 160));
  input.addEventListener('keydown', (e) => {
    const opts = $$('#search-results .res');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      searchSel = (searchSel + (e.key === 'ArrowDown' ? 1 : -1) + opts.length) % opts.length;
      opts.forEach((o, i) => o.setAttribute('aria-selected', String(i === searchSel)));
    } else if (e.key === 'Enter') {
      const o = opts[Math.max(0, searchSel)];
      if (o) chooseSearch(Number(o.dataset.i));
    } else if (e.key === 'Escape') { $('#search-results').hidden = true; }
  });
  $('#search-results').addEventListener('click', (e) => {
    const b = e.target.closest('.res'); if (b) chooseSearch(Number(b.dataset.i));
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.search')) $('#search-results').hidden = true; });
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ------------------------------------------------------------------ statistics */
function openStats() {
  const s = STATS;
  const cov = s.coverage;
  const tiles = [
    [fmt(cov.conus_mapped), `of ${fmt(cov.conus_huc8)} lower-48 watersheds mapped`],
    [fmt(s.network.km), 'km of stream with flood maps'],
    [fmt(s.network.unique_reaches), 'NWM reaches'],
    [fmt(s.flooded_km2['100']), 'km² flooded at 100-year'],
    [fmt(s.network.gaged), 'reaches with a USGS gage'],
  ];
  const bars = PERIODS.filter((p) => s.flooded_km2[String(p)] > 0).map((p) => ({
    k: `${p}-yr`, v: s.flooded_km2[String(p)], tip: `${p}-year: ${fmt(s.flooded_km2[String(p)])} km² in ${fmt(s.periods[String(p)].hucs)} watersheds` }));
  const body = `<div class="tiles">${tiles.map(([v, k]) => `<div class="tile"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('')}</div>
    <div class="grid2">
      <div><div class="section-title">Total flooded area by return period</div>${barChart(bars, 'km²')}
        <p class="note">500-year covers only the ${fmt(s.periods['500'].hucs)} LP-III watersheds, so it is not comparable with the other bars.</p></div>
      <div><div class="section-title">How the watersheds were mapped</div>
        ${table(['Method', 'Watersheds'], Object.entries(s.methods).map(([k, v]) => [k, fmt(v)]))}
        <div class="section-title">Coverage, all ${fmt(cov.huc8_total)} HUC8s</div>
        ${table(['Status', 'Watersheds'], Object.entries(STATUS).map(([k, v]) => [v.label, fmt(cov.status[k] || 0)]))}</div>
    </div>
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="true" data-tab="state">By state</button>
      <button role="tab" aria-selected="false" data-tab="huc2">By region</button>
      <button role="tab" aria-selected="false" data-tab="order">By stream order</button>
    </div>
    <div class="table-wrap" id="stats-table"></div>
    <div class="actions-row"><button class="btn" id="stats-csv">Download these tables (CSV)</button></div>`;
  $('#stats-body').innerHTML = body;
  bindChartTips($('#stats-body'));
  const show = (k) => {
    $$('#stats-body [data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === k)));
    renderStatsTable(k);
  };
  $$('#stats-body [data-tab]').forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  $('#stats-csv').addEventListener('click', downloadStatsCsv);
  show('state');
  $('#dlg-stats').showModal();
}

function statsRows(k) {
  const s = STATS;
  if (k === 'state') return {
    head: ['State', 'Watersheds', 'Reaches', 'Stream km', '2-yr km²', '100-yr km²'],
    rows: s.by_state.map((r) => [r.state, r.hucs, r.reaches, r.km, r.flooded_km2['2'], r.flooded_km2['100']]),
  };
  if (k === 'huc2') return {
    head: ['Region', 'HUC8s', 'Mapped', 'Reaches', 'Stream km', '100-yr km²'],
    rows: s.by_huc2.map((r) => [`${r.huc2} ${r.name || ''}`, r.hucs, r.mapped, r.reaches, r.km, r.flooded_km2['100'] ?? null]),
  };
  return {
    head: ['Stream order', 'Reaches', 'Stream km', 'Median 100-yr m³/s', '2-yr km²', '100-yr km²'],
    rows: s.by_order.map((r) => [r.order, r.reaches, r.km, r.q100_median, r.flooded_km2['2'], r.flooded_km2['100']]),
  };
}

let statsSort = { k: null, col: 0, dir: 1 };
function renderStatsTable(k) {
  const { head, rows } = statsRows(k);
  if (statsSort.k !== k) statsSort = { k, col: 0, dir: 1 };
  const sorted = [...rows].sort((a, b) => {
    const x = a[statsSort.col], y = b[statsSort.col];
    return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * statsSort.dir;
  });
  $('#stats-table').innerHTML = `<table class="tbl"><thead><tr>${head.map((h, i) =>
    `<th class="sortable" data-col="${i}" aria-sort="${statsSort.col === i ? (statsSort.dir > 0 ? 'ascending' : 'descending') : 'none'}">${esc(h)}${statsSort.col === i ? (statsSort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead>
    <tbody>${sorted.map((r) => `<tr>${r.map((v) => `<td>${typeof v === 'number' ? fmt(v, v % 1 ? 1 : 0) : esc(v ?? '–')}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  $$('#stats-table th').forEach((th) => th.addEventListener('click', () => {
    const col = Number(th.dataset.col);
    statsSort = { k, col, dir: statsSort.col === col ? -statsSort.dir : (col ? -1 : 1) };
    renderStatsTable(k);
  }));
}

/* ------------------------------------------------------------------ export */
function csv(head, rows) {
  const q = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  return [head, ...rows].map((r) => r.map(q).join(',')).join('\n') + '\n';
}
function downloadBlob(name, text, type = 'text/csv') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
function downloadStatsCsv() {
  const parts = ['state', 'huc2', 'order'].map((k) => {
    const { head, rows } = statsRows(k);
    return `# ${k === 'state' ? 'By state (primary state of each watershed)' : k === 'huc2' ? 'By HUC2 region' : 'By NWM stream order'}\n` + csv(head, rows);
  });
  downloadBlob('national-flood-map-statistics.csv', parts.join('\n'));
}
function downloadCatalogCsv() {
  const head = ['huc8', 'name', 'states', 'area_km2', 'status', 'method', 'periods_yr', ...PERIODS.map((p) => `flooded_${p}yr_km2`),
    'hand_release', 'ciroh_coverage_pct', 'mapped_on', 'reaches', 'stream_km', 'named_reaches', 'usgs_gages', 'huc2_name', 'huc4_name', 'huc6_name'];
  const rows = CATALOG.map((c) => [c.h, c.n, c.st, c.a, STATUS[c.s].label, c.m, (c.p || []).join(' '), ...PERIODS.map((p) => c.w?.[String(p)]),
    c.hv, c.cov, c.d, c.r, c.km, c.nm, c.g, c.h2, c.h4, c.h6]);
  downloadBlob('national-flood-map-watersheds.csv', csv(head, rows));
}

async function exportPng() {
  toast('Preparing image…');
  await new Promise((res) => { if (map.loaded() && map.areTilesLoaded()) res(); else map.once('idle', res); });
  map.triggerRepaint();
  await new Promise((res) => map.once('render', res));
  const src = map.getCanvas();
  const dpr = src.width / src.clientWidth;
  const out = document.createElement('canvas');
  out.width = src.width; out.height = src.height;
  const ctx = out.getContext('2d');
  ctx.drawImage(src, 0, 0);
  const dark = isDark();
  const ink = dark ? '#f0efec' : '#0b0b0b', ink2 = dark ? '#c3c2b7' : '#52514e', card = dark ? 'rgba(26,26,25,0.92)' : 'rgba(252,252,251,0.94)';
  const s = (v) => v * dpr;
  const periods = state.mode === 'single' ? [state.single] : PERIODS.filter((p) => state.enabled.has(p));
  const W = s(230), lh = s(20), H = s(64) + lh * periods.length + s(44);
  const x = s(16), y = out.height - H - s(16);
  ctx.fillStyle = card; roundRect(ctx, x, y, W, H, s(10)); ctx.fill();
  ctx.fillStyle = ink; ctx.font = `700 ${s(14)}px Inter, system-ui, sans-serif`; ctx.fillText('National Flood Map', x + s(12), y + s(22));
  ctx.fillStyle = ink2; ctx.font = `400 ${s(11)}px Inter, system-ui, sans-serif`;
  ctx.fillText(state.mode === 'single' ? `${state.single}-year flood extent` : 'Most frequent flood reaching each place', x + s(12), y + s(40));
  periods.forEach((p, i) => {
    const yy = y + s(54) + lh * i;
    ctx.fillStyle = state.mode === 'single' ? SINGLE[themeKey()] : RAMP[themeKey()][PERIODS.indexOf(p)];
    roundRect(ctx, x + s(12), yy, s(22), s(13), s(3)); ctx.fill();
    ctx.fillStyle = ink; ctx.font = `500 ${s(12)}px Inter, system-ui, sans-serif`; ctx.fillText(`${p}-year`, x + s(42), yy + s(11));
    ctx.fillStyle = ink2; ctx.fillText(`${CHANCE[p]} annual chance`, x + s(110), yy + s(11));
  });
  ctx.fillStyle = ink2; ctx.font = `400 ${s(9.5)}px Inter, system-ui, sans-serif`;
  const fy = y + s(60) + lh * periods.length;
  ctx.fillText('Research product, not a regulatory flood map.', x + s(12), fy);
  ctx.fillText(`${location.host}${location.pathname} · ${new Date().toISOString().slice(0, 10)}`, x + s(12), fy + s(13));
  ctx.fillText('Basemap © OpenStreetMap, OpenFreeMap; USGS', x + s(12), fy + s(26));
  out.toBlob((b) => {
    const url = URL.createObjectURL(b);
    const a = Object.assign(document.createElement('a'), { href: url, download: `flood-map-${new Date().toISOString().slice(0, 10)}.png` });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('Image saved');
  }, 'image/png');
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

/* ------------------------------------------------------------------ about */
function fillAbout() {
  const m = STATS.methods;
  const cov = STATS.coverage;
  const set = (k, v) => $$(`[data-fill="${k}"]`).forEach((el) => { el.textContent = v; });
  set('m-ciroh', fmt(m.CIROH || 0));
  set('m-lp3', fmt((m['LP-III'] || 0) + (m.Hybrid || 0)));
  set('n500', fmt(STATS.periods['500'].hucs));
  set('hand', META?.sources?.hand_release || '4.9.9.0');
  set('coverage', `${fmt(cov.conus_mapped)} of the ${fmt(cov.conus_huc8)} watersheds (HUC8) in the lower 48 states have flood extents, ` +
    `${fmt(STATS.network.km)} km of stream in ${fmt(STATS.network.unique_reaches)} NWM reaches. The remaining lower-48 HUC8s are open water or lie mostly in Canada or Mexico.`);
  set('generated', `Data generated ${STATS.generated}.`);
  $$('[data-fill-href="hf"]').forEach((a) => { a.href = CONFIG.hfPage; });
  $('#citation').textContent = META?.citation || '';
  if (META?.dictionary) {
    $('#dictionary').innerHTML = Object.entries(META.dictionary).map(([group, fields]) =>
      `<p><strong>${esc(group)}</strong></p><table class="tbl dict"><thead><tr><th>Field</th><th>Meaning</th></tr></thead><tbody>${
        Object.entries(fields).map(([f, d]) => `<tr><td><code>${esc(f)}</code></td><td>${esc(d)}</td></tr>`).join('')}</tbody></table>`).join('');
  }
}

/* ------------------------------------------------------------------ hash */
function setHashParam(k, v) {
  const p = new URLSearchParams(location.hash.slice(1));
  if (v == null) p.delete(k); else p.set(k, v);
  history.replaceState(null, '', '#' + p.toString().replace(/%2F/g, '/'));
}
function getHashParam(k) { return new URLSearchParams(location.hash.slice(1)).get(k); }

/* ------------------------------------------------------------------ boot */
async function boot() {
  loadState(); applyTheme(); syncControls();
  try {
    const [cat, stats, meta] = await Promise.all([loadJSON('data/catalog.json'), loadJSON('data/stats.json'), loadJSON('data/metadata.json').catch(() => null)]);
    CATALOG = cat.hucs; STATS = stats; META = meta;
    BY_HUC = new Map(CATALOG.map((c) => [c.h, c]));
  } catch (e) {
    toast('Could not load the map catalog. Reload the page to try again.', 8000);
    console.error(e);
  }
  initMap();
  renderLegend(); bindControls(); bindSearch(); bindHucList();
  if (STATS) fillAbout();
  const h = getHashParam('huc');
  if (h) map.once('load', () => selectHuc(h, { fly: !getHashParam('map') }));
  window.__nfm = { map, state, tileValues, LUT: () => LUT, refreshFlood };
}
boot();
