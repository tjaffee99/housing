"""Area outlines + statistics, the search index, and the manifest the web app reads.

Outputs
  data/areas/c.pmtiles, t.pmtiles, b[-n].pmtiles   county (z0-6), tract (z8), block group (z10) outlines
                                                   with id + headline stats (v/g medians, ve/ge "estimated"
                                                   flags, o/r owner and renter homes), for hover / click
  data/areas/<county GEOID>.json                   full stats + price distributions of every tract and
                                                   block group in a county (fetched on click)
  data/summary.json                                US, state and county stats + county names
  data/search.json                                 states, counties, places, ZIP codes
"""
import os, re, json, glob, pickle, subprocess, zipfile, io, shutil, datetime
import numpy as np, pandas as pd, geopandas as gpd
from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.writer import Writer
from pmtiles.tile import tileid_to_zxy, zxy_to_tileid, TileType, Compression
from common import (WORK, OUT, HERE, VALUE_EDGES, RENT_EDGES, VALUE_CLASSES, RENT_CLASSES, FINEST, SHARD_BYTES,
                    ACS_YEAR, STATES)

TIPPECANOE = os.environ.get('TIPPECANOE', 'tippecanoe')
AREAS = os.path.join(OUT, 'areas')

def interp_median(counts, edges):
    """Census-style linear interpolation of the median from binned counts.
    Returns (median, open_ended). A median in the open top bracket comes back as its lower edge + 1,
    the same top-code convention ACS uses for published medians (2,000,001 = "$2,000,000+")."""
    counts = np.nan_to_num(counts)
    cum = np.cumsum(counts, axis=1)
    tot = cum[:, -1]
    half = tot / 2.0
    k = np.argmax(cum >= half[:, None], axis=1)
    lo = np.asarray(edges, float)[k]
    hi_edges = np.asarray(list(edges[1:]) + [np.nan], float)
    hi = hi_edges[k]
    before = np.where(k > 0, np.take_along_axis(cum, np.maximum(k - 1, 0)[:, None], 1)[:, 0], 0.0)
    inbin = np.take_along_axis(counts, k[:, None], 1)[:, 0]
    med = lo + (half - before) / np.maximum(inbin, 1e-9) * (hi - lo)
    top = np.isnan(hi)
    med = np.where(top, lo + 1, med)
    med = np.where(tot > 0, med, np.nan)
    return med, top & (tot > 0)

def classes(df, prefix, cls):
    return np.stack([df[[f'{prefix}{i}' for i in range(lo, hi + 1)]].fillna(0).sum(axis=1).to_numpy()
                     for _, lo, hi in cls], 1)

def enrich(df):
    """Add headline medians (published, else interpolated) and class distributions."""
    df = df.copy()
    V = df[[f'v{i}' for i in range(26)]].to_numpy(float)
    R = df[[f'r{i}' for i in range(24)]].to_numpy(float)
    iv, _ = interp_median(V, VALUE_EDGES)
    ir, _ = interp_median(R, RENT_EDGES)
    df['v_est'] = df.med_value.isna() & ~np.isnan(iv)
    df['g_est'] = df.med_rent.isna() & ~np.isnan(ir)
    df['v'] = df.med_value.fillna(pd.Series(iv, index=df.index))
    df['g'] = df.med_rent.fillna(pd.Series(ir, index=df.index))
    df['vc'] = list(np.rint(classes(df, 'v', VALUE_CLASSES)).astype(int))
    df['rc'] = list(np.rint(classes(df, 'r', RENT_CLASSES)).astype(int))
    return df

def num(x, nd=0):
    if x is None or (isinstance(x, float) and np.isnan(x)): return None
    return int(round(x)) if nd == 0 else round(float(x), nd)

def record(row):
    """Compact stats array (see FIELDS in manifest)."""
    return [num(row.owners), num(row.renters), num(row.units), num(row.vacant),
            num(row.v), num(row.med_value_moe), 1 if row.v_est else 0,
            num(row.g), num(row.med_rent_moe), 1 if row.g_est else 0,
            num(row.med_income), num(row.med_income_moe), num(row.rent_burden, 1),
            [int(c) for c in row.vc], [int(c) for c in row.rc], int(getattr(row, 'synth', 0) or 0)]

FIELDS = ['owners', 'renters', 'units', 'vacant', 'value', 'value_moe', 'value_est', 'rent', 'rent_moe', 'rent_est',
          'income', 'income_moe', 'rent_burden', 'value_classes', 'rent_classes', 'synth']

def run_tippecanoe(src, dst, layer, zmin, zmax, extra=()):
    cmd = [TIPPECANOE, '-q', '-f', '-o', dst, '-l', layer, '-Z', str(zmin), '-z', str(zmax), '-P',
           '--detect-shared-borders', '--no-feature-limit', '--no-tile-size-limit', '--no-tiny-polygon-reduction',
           *extra, src]
    subprocess.run(cmd, check=True)

def shard(path, base):
    """Split a single-zoom PMTiles archive into tile-column shards under SHARD_BYTES each."""
    with open(path, 'rb') as f:
        src = MmapSource(f)
        r = Reader(src)
        header = r.header(); meta = r.metadata()
        tiles = [(zxy_to_tileid(z, x, y), x, data) for (z, x, y), data in all_tiles(src)]
    total = sum(len(t[2]) for t in tiles)
    z = header['max_zoom']
    n = max(1, -(-total // SHARD_BYTES))
    if n == 1:
        return [{'file': os.path.basename(path), 'x0': 0, 'x1': (1 << z) - 1}]
    cols = {}
    for t in tiles: cols[t[1]] = cols.get(t[1], 0) + len(t[2])
    acc, cuts = 0, []
    for x in sorted(cols):
        acc += cols[x]
        if acc >= total / n * (len(cuts) + 1) and len(cuts) < n - 1: cuts.append(x)
    bounds = [0] + [c + 1 for c in cuts] + [1 << z]
    out = []
    for i in range(len(bounds) - 1):
        x0, x1 = bounds[i], bounds[i + 1] - 1
        fn = f'{base}-{i}.pmtiles'
        with open(os.path.join(os.path.dirname(path), fn), 'wb') as f:
            w = Writer(f)
            for tid, x, data in sorted(tiles):
                if x0 <= x <= x1: w.write_tile(tid, data)
            hdr = {k: header[k] for k in ('tile_type', 'tile_compression', 'min_lon_e7', 'min_lat_e7', 'max_lon_e7',
                                          'max_lat_e7', 'center_zoom', 'center_lon_e7', 'center_lat_e7')}
            w.finalize(hdr, meta)
        out.append({'file': fn, 'x0': x0, 'x1': x1})
    os.remove(path)
    return out

def main():
    acs = pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))
    lv = {k: enrich(acs[k]) for k in ('us', 'state', 'county', 'tract', 'bg')}
    pickle.dump(lv['us'], open(os.path.join(WORK, 'us.pkl'), 'wb'))
    os.makedirs(AREAS, exist_ok=True)
    json.dump({'fields': FIELDS}, open(os.path.join(WORK, 'fields.json'), 'w'))
    for f in glob.glob(os.path.join(AREAS, '*')): os.remove(f)

    # ---------------------------------------------------------------- polygons -> tiles
    tmp = os.path.join(WORK, 'areas'); os.makedirs(tmp, exist_ok=True)
    states = gpd.read_file(os.path.join(WORK, 'geo', 'cb_state.zip'))
    states = states[states.GEOID.isin(STATES)]      # no ACS 5-year data for the island areas
    st_abbr = dict(zip(states.GEOID, states.STUSPS)); st_name = dict(zip(states.GEOID, states.NAME))
    counties = gpd.read_file(os.path.join(WORK, 'geo', 'cb_county.zip'))
    cname = dict(zip(counties.GEOID, counties.NAMELSAD))
    files = {}
    for key, fn, layer, zmin, zmax in (('county', 'cb_county.zip', 'c', 0, 6), ('tract', 'cb_tract.zip', 't', 8, 8),
                                       ('bg', 'cb_bg.zip', 'b', 10, 10)):
        g = gpd.read_file(os.path.join(WORK, 'geo', fn), columns=['GEOID'])
        g = g[g.GEOID.str[:2].isin(set(st_abbr))]
        s = lv[key].reindex(g.GEOID)
        have = s.owners.notna().to_numpy()
        if (~have).any():
            print(f'warning: {(~have).sum()} {key} outlines have no ACS row; they get no stats')
        out = gpd.GeoDataFrame({'id': g.GEOID.to_numpy()}, geometry=g.geometry.to_numpy(), crs=g.crs).to_crs(4326)
        def ints(v): return pd.array([int(round(x)) if np.isfinite(x) else None for x in v], dtype='Int64')
        out['o'] = ints(s.owners.to_numpy())
        out['r'] = ints(s.renters.to_numpy())
        out['v'] = ints(s.v.to_numpy())
        out['g'] = ints(s.g.to_numpy())
        out['ve'] = pd.array([1 if f is True else None for f in s.v_est], dtype='Int64')
        out['ge'] = pd.array([1 if f is True else None for f in s.g_est], dtype='Int64')
        if 'synth' in s:   # 1 = no published estimate, 2 = partly estimated
            out['x'] = pd.array([int(f) if f in (1, 2) else None for f in s.synth], dtype='Int64')
        src_path = os.path.join(tmp, key + '.geojsonl')
        out.to_file(src_path, driver='GeoJSONSeq')
        dst = os.path.join(AREAS, layer + '.pmtiles')
        run_tippecanoe(src_path, dst, layer, zmin, zmax, ['--simplification=2'] if key == 'county' else [])
        files[key] = {'layer': layer, 'minzoom': zmin, 'maxzoom': zmax, 'shards': shard(dst, layer)}
        print(key, len(out), 'features ->', [(sh['file'], round(os.path.getsize(os.path.join(AREAS, sh['file'])) / 1e6, 1))
                                             for sh in files[key]['shards']], flush=True)

    # ---------------------------------------------------------------- per-county detail shards
    recs = {}
    for key in ('tract', 'bg'):
        for i, r in zip(lv[key].index, lv[key].itertuples()):
            recs.setdefault(i[:5], {'tract': {}, 'bg': {}})[key][i] = record(r)
    n = 0
    for cid in lv['county'].index:
        with open(os.path.join(AREAS, cid + '.json'), 'w') as f:
            json.dump(recs.get(cid, {'tract': {}, 'bg': {}}), f, separators=(',', ':'))
        n += 1
    print(n, 'county detail files')
    json.dump(files, open(os.path.join(WORK, 'areas.json'), 'w'), indent=1)

    # ---------------------------------------------------------------- summary
    summary = {'fields': FIELDS,
               'us': record(next(lv['us'].itertuples())),
               'state': {i: [st_abbr.get(i, ''), st_name.get(i, '')] + [record(r)] for i, r in zip(lv['state'].index, lv['state'].itertuples())},
               'county': {i: [cname.get(i, i)] + [record(r)] for i, r in zip(lv['county'].index, lv['county'].itertuples())}}
    with open(os.path.join(OUT, 'summary.json'), 'w') as f:
        json.dump(summary, f, separators=(',', ':'))

    # ---------------------------------------------------------------- search index
    # Each county, place and ZIP is centered on where its homes are (median of a sample of dots that
    # fall inside it) and zoomed to fit the middle 80% of them, so a search never lands on empty
    # land or open water (San Francisco's official internal point is in the Pacific).
    hh = pd.read_csv(os.path.join(WORK, 'acs', 'b25003.dat'), sep='|', dtype=str, usecols=['GEO_ID', 'B25003_E001'])
    hh = hh.set_index('GEO_ID').B25003_E001.pipe(pd.to_numeric, errors='coerce')
    dots = {k: [] for k in 'xyg'}
    for f in sorted(glob.glob(os.path.join(WORK, 'dots', '*.npz'))):
        d = np.load(f)
        keep = d['u'] < 0.25
        for k in dots: dots[k].append(d[k][keep])
    dots = {k: np.concatenate(v) for k, v in dots.items()}
    n24 = float(1 << 24)
    lon = dots['x'] / n24 * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * dots['y'] / n24))))
    def frame(keys, x, y, zmin, zmax):
        """Housing-weighted center and zoom per key: the home nearest the median position (so the view
        always has homes in the middle), zoomed to fit the 10th-90th percentile box. Areas with only a
        handful of sampled homes open at street level, where every home is drawn."""
        df = pd.DataFrame({'k': keys, 'x': x, 'y': y})
        g = df.groupby('k')
        q = g.quantile([0.1, 0.5, 0.9]).unstack()
        n = g.size()
        span = np.maximum(q[('x', 0.9)] - q[('x', 0.1)], q[('y', 0.9)] - q[('y', 0.1)]).clip(lower=1)
        zoom = np.clip(np.log2(480 * n24 / (512 * span)), zmin, zmax)
        zoom = np.where(n < 25, np.maximum(zoom, 12.3), zoom)
        df['d'] = (df.x - q[('x', 0.5)].reindex(df.k).to_numpy()) ** 2 + (df.y - q[('y', 0.5)].reindex(df.k).to_numpy()) ** 2
        near = df.loc[df.groupby('k').d.idxmin()].set_index('k')
        mx, my = near.x.reindex(q.index), near.y.reindex(q.index)
        c_lon = mx / n24 * 360 - 180
        c_lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * my / n24))))
        return pd.DataFrame({'lat': c_lat.round(4), 'lon': c_lon.round(4), 'zoom': np.round(zoom, 1), 'n': n})
    bgids = np.asarray(pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))['bg'].index)
    county_of = pd.Series(bgids).str[:5].to_numpy()[dots['g']]
    cframe = frame(county_of, dots['x'].astype(float), dots['y'].astype(float), 6, 12)
    pts = gpd.GeoDataFrame({'i': np.arange(len(lon))}, geometry=gpd.points_from_xy(lon, lat), crs=4326)
    def framed(polys, idcol, zmin, zmax):
        polys = polys.to_crs(4326)[[idcol, 'geometry']]
        j = gpd.sjoin(pts, polys, predicate='within', how='inner')
        return frame(j[idcol].to_numpy(), dots['x'][j.i.to_numpy()].astype(float), dots['y'][j.i.to_numpy()].astype(float), zmin, zmax)
    places = gpd.read_file(os.path.join(WORK, 'geo', 'cb_place.zip'))
    places = places[places.STATEFP.isin(STATES)]
    pframe = framed(places, 'GEOID', 8, 14)
    zctas = gpd.read_file(os.path.join(WORK, 'geo', 'cb_zcta.zip'))
    zframe = framed(zctas, 'GEOID20', 10, 14)
    print('framed', len(cframe), 'counties,', len(pframe), 'places,', len(zframe), 'ZIPs')

    # state boxes: Alaska's Aleutians cross the 180th meridian and Hawaii's include the uninhabited
    # Northwestern Islands, so both get boxes around where people live
    BOX = {'02': [-179.2, 51.2, -129.9, 71.4], '15': [-160.3, 18.85, -154.75, 22.3]}
    # entries: [name, state | 'State' | 'ZIP', lat, lon, zoom, bbox or 0, kind (s/c/p/z), GEOID for states and
    # counties (opens their details), note to tell same-name places apart]
    items = []
    for _, r in states.iterrows():
        b = BOX.get(r.GEOID, [round(v, 3) for v in r.geometry.bounds])
        items.append([r.NAME, 'State', round((b[1] + b[3]) / 2, 4), round((b[0] + b[2]) / 2, 4), 0, b, 's', r.GEOID, '', 1e9])
    county_names = set()
    for _, r in counties[counties.GEOID.str[:2].isin(STATES)].iterrows():
        if r.GEOID not in cframe.index: continue
        f = cframe.loc[r.GEOID]
        county_names.add((r.NAMELSAD, r.STUSPS))
        items.append([r.NAMELSAD, r.STUSPS, f.lat, f.lon, f.zoom, 0, 'c', r.GEOID, '', float(hh.get('0500000US' + r.GEOID, 0) or 0) * 0.5])
    # consolidated city-counties' "balance" parts: 'Indianapolis city (balance)' -> 'Indianapolis'
    balance = re.compile(r'\s+(?:city and borough|(?:consolidated|metropolitan|metro|unified) government|urban county|'
                         r'city|town|village|borough|municipality)?\s*\(balance\)$')
    pl = []
    for _, r in places.iterrows():
        if r.GEOID not in pframe.index: continue
        h = hh.get('1600000US' + r.GEOID, np.nan)
        if not np.isfinite(h):        # no ACS row (e.g. Brentwood, NY): rank by its homes on the map
            h = pframe.loc[r.GEOID, 'n'] * 40.0
        if h <= 0: continue
        name = balance.sub('', r.NAME)
        if (name, r.STUSPS) in county_names: continue     # consolidated city-counties: keep the county entry
        f = pframe.loc[r.GEOID]
        pl.append([name, r.STUSPS, f.lat, f.lon, f.zoom, 0, 'p', '', '', float(h)])
    # same-name places in one state get their county as a note
    dup = pd.Series([(p[0], p[1]) for p in pl]).duplicated(keep=False).to_numpy()
    if dup.any():
        cpts = gpd.GeoDataFrame({'i': np.flatnonzero(dup)}, geometry=gpd.points_from_xy([pl[i][3] for i in np.flatnonzero(dup)],
                                [pl[i][2] for i in np.flatnonzero(dup)]), crs=4326)
        cj = gpd.sjoin(cpts, counties.to_crs(4326)[['NAMELSAD', 'geometry']], predicate='within', how='left')
        for i, cn in zip(cj.i, cj.NAMELSAD):
            if isinstance(cn, str): pl[i][8] = cn
    items += pl
    zhh = {k[-5:]: v for k, v in hh.items() if k.startswith('860')}
    for z5, f in zframe.iterrows():
        items.append([z5, 'ZIP', f.lat, f.lon, f.zoom, 0, 'z', '', '', float(zhh.get(z5, 0) or 0) * 0.01])
    items.sort(key=lambda it: -it[9])
    with open(os.path.join(OUT, 'search.json'), 'w') as f:
        json.dump([it[:9] if it[8] else it[:8] if it[7] else it[:7] for it in items], f, separators=(',', ':'), ensure_ascii=False)
    print(len(items), 'search entries')


if __name__ == '__main__':
    main()
