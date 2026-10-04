"""Polygons the web app uses to set the US apart from the rest of the base map.

Input   work/geo/cb_state.zip    (Census cartographic boundary, cb_2024_us_state_500k; the 52 covered states)
        work/dots/*.npz          (so no home ends up under the mask)
Output  data/mask.json   world minus the US, as hole-free polygons, drawn over foreign land and sea.
                         (MapLibre clips GeoJSON into tiles; a polygon with holes clipped at a tile
                         edge can fill the whole tile, which showed up as dark squares over US land.)
        data/us.json     a US outline for `within` filters (foreign place names are dimmed), kept tight
                         at land borders so Tijuana or Windsor don't count as US.
"""
import os, json, glob
import numpy as np, geopandas as gpd, shapely
from shapely.geometry import box, mapping
from common import WORK, OUT, STATES

def no_holes(p):
    """Split a polygon with holes into hole-free pieces by cutting through its first hole."""
    if not p.interiors:
        return [p]
    c = shapely.Polygon(p.interiors[0]).representative_point()
    x0, y0, x1, y1 = p.bounds
    out = []
    for half in (box(x0 - 1, y0 - 1, c.x, y1 + 1), box(c.x, y0 - 1, x1 + 1, y1 + 1)):
        for q in shapely.get_parts(p.intersection(half)):
            if q.geom_type == 'Polygon' and q.area > 1e-9:
                out += no_holes(q)
    return out

def rounded(geom, nd=3):
    return json.loads(json.dumps(mapping(geom)), parse_float=lambda s: round(float(s), nd))

def main():
    st = gpd.read_file(os.path.join(WORK, 'geo', 'cb_state.zip'))
    land = shapely.make_valid(st[st.GEOID.isin(STATES)].to_crs(4326).geometry.union_all())
    us = shapely.simplify(land.buffer(0.02), 0.01)            # a little breathing room past the coast
    xs, ys = [], []
    for f in sorted(glob.glob(os.path.join(WORK, 'dots', '*.npz'))):
        d = np.load(f)
        xs.append(d['x']); ys.append(d['y'])
    n = float(1 << 24)
    x = np.concatenate(xs) / n * 360 - 180
    y = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.concatenate(ys) / n))))
    # keep any island with homes on it, however small; drop specks of rock
    parts = list(shapely.get_parts(us))
    tree = shapely.STRtree(parts)
    hit = set(tree.query(shapely.points(x[::20], y[::20]), predicate='within')[1].tolist())
    parts = [p for i, p in enumerate(parts) if p.area > 0.002 or i in hit]
    us = shapely.MultiPolygon(parts)
    # homes on spits and islets the generalized coastline misses get a small disk of their own
    out = ~shapely.contains_xy(us, x, y)
    if out.any():
        us = shapely.union_all([us, *shapely.buffer(shapely.points(x[out], y[out]), 0.015, quad_segs=4)])
        print(f'{out.sum()} dots outside the coastline outline get their own patch')
    mask = box(-180, -85, 180, 85).difference(us)
    pieces = [q for p in shapely.get_parts(mask) for q in no_holes(p)]
    pieces = [shapely.set_precision(p, 0.001) for p in pieces]
    pieces = [q for p in pieces for q in shapely.get_parts(p) if q.geom_type == 'Polygon' and q.area > 1e-7]
    assert all(not p.interiors for p in pieces)
    with open(os.path.join(OUT, 'mask.json'), 'w') as f:
        json.dump({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {}, 'geometry': rounded(p)} for p in pieces]},
                  f, separators=(',', ':'))
    coarse = shapely.simplify(land.buffer(0.008), 0.004)
    with open(os.path.join(OUT, 'us.json'), 'w') as f:
        json.dump({"type": "Feature", "properties": {}, "geometry": rounded(coarse, 3)}, f, separators=(',', ':'))
    lost = int((~shapely.contains_xy(us, x, y)).sum())
    print(f'mask: {len(pieces)} hole-free pieces, {os.path.getsize(os.path.join(OUT, "mask.json")) // 1000} KB; '
          f'us.json {os.path.getsize(os.path.join(OUT, "us.json")) // 1000} KB; dots under the mask: {lost}')

if __name__ == '__main__':
    main()
