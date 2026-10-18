"""Area outlines with statistics, the per-county detail files and the national summary.

Outputs
  data/areas/c.pmtiles, t.pmtiles, b[-n].pmtiles   county (z0-6), tract (z8) and block group (z10) outlines with
                                                   headline medians for hover and the area-median view:
                                                   v value, p paid rent, m market rent, c monthly cost (everyone),
                                                   co monthly cost of buying, o/r owner/renter homes, x estimated
  data/areas/z.pmtiles                             ZIP code outlines (z3-8) with the same medians plus typical
                                                   prices by size: v1-v5 home value (Zillow), m0-m4 market rent
                                                   (HUD Fair Market Rent), p0-p4 rent paid (ACS), c1-c5 monthly
                                                   cost of buying (from v1-v5), zhvi/zori Zillow's typical value
                                                   and asking rent, cn main county
  data/areas/<county GEOID>.json                   full records of every tract and block group in a county
  data/summary.json                                US, state and county records + names
"""
import os, json, glob, pickle, subprocess
import numpy as np, pandas as pd, geopandas as gpd
from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.writer import Writer
from pmtiles.tile import zxy_to_tileid
from common import WORK, OUT, SHARD_BYTES, STATES

TIPPECANOE = os.environ.get('TIPPECANOE', 'tippecanoe')
AREAS = os.path.join(OUT, 'areas')

# compact record per area (the order the app reads, see manifest 'fields')
FIELDS = ['owners', 'renters', 'units', 'vacant', 'v', 'p', 'm', 'c', 'co', 'income', 'rent_burden', 'tax',
          'v_moe', 'p_moe', 'hv', 'hp', 'hm', 'hco', 'synth', 'mratio', 'income_top']


def num(x, nd=0):
    if x is None: return None
    try:
        if not np.isfinite(x): return None
    except TypeError:
        return None
    return int(round(float(x))) if nd == 0 else round(float(x), nd)


def records(stats, acs, lvl):
    """Area records as lists in FIELDS order."""
    s = stats[lvl]
    a = acs[lvl] if lvl != 'state' else acs['state'].reindex(STATES)
    a = a.reindex(s.index) if lvl != 'us' else a.set_axis(s.index)
    def rel(med, moe, top):
        r = (a[moe] / a[med]).where((a[med] > 0) & (a[med] < top))
        return r.to_numpy()
    vm = rel('med_value', 'med_value_moe', 2000001)
    pm = rel('med_rent', 'med_rent_moe', 3501)
    mr = s['mratio'].to_numpy() if 'mratio' in s else np.full(len(s), np.nan)
    itop = (a.med_income >= 250001).to_numpy()          # ACS top code: "$250,000 or more" (2024 dollars)
    out = {}
    for i, (gid, r) in enumerate(s.iterrows()):
        out[gid] = [num(r.owners), num(r.renters), num(r.units), num(r.vacant), num(r.v),
                    num(r.p), num(r.m), num(r.c), num(r.co), num(r.income), num(r.rent_burden, 1),
                    num(r.tax * 100 if r.tax is not None else None, 2), num(vm[i], 3), num(pm[i], 3),
                    [int(round(x)) for x in r.hv], [int(round(x)) for x in r.hp], [int(round(x)) for x in r.hm],
                    [int(round(x)) for x in r.hco], int(r.synth or 0), num(mr[i], 3), 1 if itop[i] else 0]
    return out


def run_tippecanoe(src, dst, layer, zmin, zmax, extra=()):
    cmd = [TIPPECANOE, '-q', '-f', '-o', dst, '-l', layer, '-Z', str(zmin), '-z', str(zmax), '-P',
           '--detect-shared-borders', '--no-feature-limit', '--no-tile-size-limit', '--no-tiny-polygon-reduction',
           *extra, src]
    subprocess.run(cmd, check=True)


def shard(path, base):
    """Split a PMTiles archive into tile-column shards under SHARD_BYTES each (single-zoom archives only)."""
    with open(path, 'rb') as f:
        src = MmapSource(f)
        r = Reader(src)
        header = r.header(); meta = r.metadata()
        tiles = [(zxy_to_tileid(z, x, y), x, data) for (z, x, y), data in all_tiles(src)]
    total = sum(len(t[2]) for t in tiles)
    z = header['max_zoom']
    n = max(1, -(-total // SHARD_BYTES))
    if n == 1 or header['min_zoom'] != z:
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


def ints(v):
    return pd.array([int(round(x)) if np.isfinite(x) else None for x in np.asarray(v, float)], dtype='Int64')


def main():
    acs = pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))
    stats = pickle.load(open(os.path.join(WORK, 'now', 'stats.pkl'), 'rb'))
    os.makedirs(AREAS, exist_ok=True)
    for f in glob.glob(os.path.join(AREAS, '*')): os.remove(f)
    tmp = os.path.join(WORK, 'areas'); os.makedirs(tmp, exist_ok=True)
    states = gpd.read_file(os.path.join(WORK, 'geo', 'cb_state.zip'), ignore_geometry=True)
    states = states[states.GEOID.isin(STATES)]
    st_abbr = dict(zip(states.GEOID, states.STUSPS)); st_name = dict(zip(states.GEOID, states.NAME))
    counties = gpd.read_file(os.path.join(WORK, 'geo', 'cb_county.zip'), ignore_geometry=True)
    cname = dict(zip(counties.GEOID, counties.NAMELSAD))

    # ---------------------------------------------------------------- outlines -> tiles
    files = {}
    for key, fn, idcol, layer, zmin, zmax in (('county', 'cb_county.zip', 'GEOID', 'c', 0, 6),
                                              ('tract', 'cb_tract.zip', 'GEOID', 't', 8, 8),
                                              ('bg', 'cb_bg.zip', 'GEOID', 'b', 10, 10),
                                              ('zcta', 'cb_zcta.zip', 'ZCTA5CE20', 'z', 3, 8)):
        g = gpd.read_file(os.path.join(WORK, 'geo', fn), columns=[idcol])
        if key != 'zcta':
            g = g[g[idcol].str[:2].isin(set(st_abbr))]
        s = stats[key].reindex(g[idcol])
        have = (s.owners + s.renters).fillna(0).to_numpy() > 0
        if key != 'zcta' and (~have).any():
            print(f'warning: {(~have).sum()} {key} outlines have no homes; they get no stats')
        out = gpd.GeoDataFrame({'id': g[idcol].to_numpy()}, geometry=g.geometry.to_numpy(), crs=g.crs).to_crs(4326)
        out['o'] = ints(s.owners); out['r'] = ints(s.renters)
        for col in ('v', 'p', 'm', 'c', 'co'):
            out[col] = ints(s[col])
        if 'synth' in s:   # 1 = no published estimate, 2 = partly estimated
            out['x'] = pd.array([int(f) if f in (1, 2) else None for f in s.synth.fillna(0)], dtype='Int64')
        if key == 'zcta':
            sz = stats['sizes'].reindex(g[idcol])
            for col in [f'v{b}' for b in range(1, 6)] + [f'm{b}' for b in range(5)] + [f'p{b}' for b in range(5)] + \
                       [f'c{b}' for b in range(1, 6)] + ['zhvi', 'zori']:
                out[col] = ints(sz[col])
            out['cn'] = sz['county'].fillna('').to_numpy()
            out = out[have | sz[['v2', 'm2']].notna().any(axis=1).to_numpy()]
        src_path = os.path.join(tmp, key + '.geojsonl')
        out.to_file(src_path, driver='GeoJSONSeq')
        dst = os.path.join(AREAS, layer + '.pmtiles')
        extra = {'county': ['--simplification=2'], 'zcta': ['--simplification=2']}.get(key, [])
        run_tippecanoe(src_path, dst, layer, zmin, zmax, extra)
        files[key] = {'layer': layer, 'minzoom': zmin, 'maxzoom': zmax, 'shards': shard(dst, layer)}
        print(key, len(out), 'features ->', [(sh['file'], round(os.path.getsize(os.path.join(AREAS, sh['file'])) / 1e6, 1))
                                             for sh in files[key]['shards']], flush=True)
    json.dump(files, open(os.path.join(WORK, 'areas.json'), 'w'), indent=1)
    json.dump({'fields': FIELDS}, open(os.path.join(WORK, 'fields.json'), 'w'))

    # ---------------------------------------------------------------- per-county detail files
    recs = {}
    for key in ('tract', 'bg'):
        for gid, rec in records(stats, acs, key).items():
            recs.setdefault(gid[:5], {'tract': {}, 'bg': {}})[key][gid] = rec
    for cid in stats['county'].index:
        with open(os.path.join(AREAS, cid + '.json'), 'w') as f:
            json.dump(recs.get(cid, {'tract': {}, 'bg': {}}), f, separators=(',', ':'))
    print(len(stats['county']), 'county detail files')

    # ---------------------------------------------------------------- summary
    cr = records(stats, acs, 'county'); sr = records(stats, acs, 'state')
    summary = {'fields': FIELDS,
               'us': records(stats, acs, 'us')['us'],
               'state': {i: [st_abbr.get(i, ''), st_name.get(i, ''), sr[i]] for i in STATES},
               'county': {i: [cname.get(i, i), cr[i]] for i in stats['county'].index}}
    with open(os.path.join(OUT, 'summary.json'), 'w') as f:
        json.dump(summary, f, separators=(',', ':'))
    print('summary.json', os.path.getsize(os.path.join(OUT, 'summary.json')) // 1000, 'KB')


if __name__ == '__main__':
    main()
