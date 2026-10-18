"""Turn ACS block-group distributions into dots: one dot per FINEST homes.

Each dot is an owner-occupied home (colored by its ACS value bin) or a renter-occupied
home (colored by its ACS gross-rent bin). A block group's dots are drawn bin by bin from
its own distribution (stochastic rounding keeps totals unbiased). Each dot then goes to one
of the block group's 2020 census blocks, picked in proportion to the block's 2020 housing-unit
count, and onto a real residential building in that block (FEMA/ORNL USA Structures), picked
in proportion to the homes the building likely holds: one for a house or manufactured home
(more for a footprint big enough to be a row of houses), floor area / 95 m^2 for an apartment
building. Renters lean toward apartment buildings and owners toward houses (UNIT_FIT). A dot
sits within a few meters of its building's center, so large buildings show as clusters. Blocks
with no mapped residential building use unclassified structures, and failing those a random
point in the block.

Inputs  work/acs.pkl, work/blocks/<state>.zip (TIGER/Line 2024 TABBLOCK20, with HOUSING20),
        work/structs/<state>.npz (tools/fetch_structures.py; optional: without it, random points in blocks)
Output  work/dots/<state>.npz
"""
import os, sys, pickle, time
import multiprocessing as mp
import numpy as np, pandas as pd, pyogrio, shapely
from common import WORK, STATES, FINEST, VALUE_EDGES, RENT_EDGES

DOTS = os.path.join(WORK, 'dots')
# relative pull of each building kind (single-family, multi-family, manufactured, other residential,
# unclassified) on owner and renter dots: renters still land in houses where a block has no
# apartments, but prefer apartments where it has both
UNIT_FIT = {0: np.array([1.0, 0.35, 1.0, 0.7, 0.7]), 1: np.array([0.35, 1.0, 0.6, 0.7, 0.7])}
M_PER_DEG = 111_320.0


def load_buildings(st, geoms):
    """Residential buildings of a state assigned to blocks (indices into `geoms`), with an estimate of
    the homes each holds. Returns None when the state's structures haven't been downloaded."""
    path = os.path.join(WORK, 'structs', st + '.npz')
    if not os.path.exists(path):
        return None
    b = np.load(path)
    lon, lat, kind, sqm, h = b['lon'], b['lat'], b['kind'], b['sqm'].astype(float), b['height'].astype(float)
    sqm = np.where(np.isfinite(sqm) & (sqm > 0), sqm, 150.0)
    floors = np.where(np.isfinite(h) & (h > 0), np.clip(h / 3.2, 1, 80), 3.0)
    units = np.where(kind == 1, np.clip(sqm * floors / 95.0, 2, 3000),
                     np.where(kind == 2, 1.0, np.clip(sqm / 300.0, 1, 20)))
    units = np.where((kind == 4) & (sqm < 40), 0.0, units)          # sheds and specks
    tree = shapely.STRtree(geoms)
    pi, bi = tree.query(shapely.points(lon, lat), predicate='within')
    first = np.unique(pi, return_index=True)[1]                    # a point on a shared edge: first block
    pi, bi = pi[first], bi[first]
    o = np.argsort(bi, kind='stable')
    return dict(block=bi[o], lon=lon[pi][o], lat=lat[pi][o], kind=kind[pi][o], units=units[pi][o],
                radius=np.clip(np.sqrt(sqm[pi][o] / np.pi), 3.0, 60.0))


def building_weights(B, tenure, nblocks):
    """Per-building weight for one tenure (unclassified structures count only in blocks without any
    residential building), and cumulative sums with per-block start/end."""
    res = B['kind'] < 4
    has_res = np.bincount(B['block'][res], minlength=nblocks) > 0
    w = B['units'] * UNIT_FIT[tenure][B['kind']]
    w = np.where((B['kind'] == 4) & has_res[B['block']], 0.0, w)
    cw = np.cumsum(w)
    start = np.searchsorted(B['block'], np.arange(nblocks)); end = np.searchsorted(B['block'], np.arange(nblocks), 'right')
    tot = np.where(end > start, cw[np.maximum(end - 1, 0)] - np.where(start > 0, cw[np.maximum(start - 1, 0)], 0.0), 0.0)
    return cw, start, end, tot

def stochastic_round(x, rng):
    fl = np.floor(x)
    return (fl + (rng.random(x.shape) < (x - fl))).astype(np.int64)

def median_bin(med, counts, edges):
    """Bin holding the block group's median: the published ACS median when there is one,
    else the bin where the cumulative distribution crosses one half."""
    out = np.searchsorted(np.asarray(edges, float), med, side='right') - 1
    cum = np.cumsum(counts, axis=1)
    tot = cum[:, -1:]
    fallback = np.argmax(cum >= tot / 2.0, axis=1)
    out = np.where(np.isfinite(med), out, fallback)
    return np.where(tot[:, 0] > 0, out, 0).astype(np.uint8)

def mercator24(lon, lat):
    n = float(1 << 24)
    x = (lon + 180.0) / 360.0 * n
    lat = np.clip(lat, -85.05112878, 85.05112878)
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n
    return np.floor(x).astype(np.uint32), np.floor(y).astype(np.uint32)

def process(st):
    t0 = time.time()
    acs = pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))['bg']
    gidx_all = pd.Series(np.arange(len(acs)), index=acs.index)
    a = acs[acs.index.str[:2] == st]
    rng = np.random.default_rng(20240 + int(st))

    blocks = pyogrio.read_dataframe(os.path.join(WORK, 'blocks', st + '.zip'),
                                    columns=['GEOID20', 'HOUSING20', 'ALAND20'])
    key = blocks.GEOID20.str[:12].to_numpy()
    if st == '09':
        # Connecticut: ACS 2022+ uses planning regions as county equivalents; 2020 blocks use the
        # old counties. Tract+block-group codes are unique statewide for every populated BG.
        m = {g[5:]: g for g in a.index[(a.owners.fillna(0) + a.renters.fillna(0)) > 0]}
        key = np.array([m.get(k[5:], k) for k in key], dtype=object)
    order = np.argsort(key, kind='stable')
    key = key[order]
    geoms = np.asarray(blocks.geometry.values)[order]
    housing = blocks.HOUSING20.to_numpy(np.float64)[order]
    aland = blocks.ALAND20.to_numpy(np.float64)[order]
    shapely.prepare(geoms)
    bounds = shapely.bounds(geoms)
    B = load_buildings(st, geoms)
    on_building = 0

    ukeys, bstart, bcount = np.unique(key, return_index=True, return_counts=True)
    pos = pd.Series(np.arange(len(ukeys)), index=ukeys)

    V = a[[f'v{i}' for i in range(26)]].fillna(0).to_numpy()
    R = a[[f'r{i}' for i in range(25)]].fillna(0).to_numpy()
    mv = median_bin(a.med_value.to_numpy(), V, VALUE_EDGES)
    mr = median_bin(a.med_rent.to_numpy(), R[:, :24], RENT_EDGES)

    xs, ys, ts, bs, ms, us, gs = [], [], [], [], [], [], []
    missing = 0
    stats = {}
    for tenure, C, med in ((0, V, mv), (1, R, mr)):
        n = stochastic_round(C / FINEST, rng)                    # dots per (bg, bin)
        row = np.repeat(np.repeat(np.arange(len(a)), C.shape[1]), n.ravel())
        b = np.tile(np.arange(C.shape[1]), len(a)).repeat(n.ravel()).astype(np.uint8)
        # nesting priority: stratified within each (bg, tenure) so every coarser tier keeps
        # floor/ceil(f * n) of a block group's dots, chosen at random
        perm = np.lexsort((rng.random(len(row)), row))
        row, b = row[perm], b[perm]
        gcount = np.bincount(row, minlength=len(a))
        gstart = np.concatenate([[0], np.cumsum(gcount)[:-1]])
        rank = np.arange(len(row)) - gstart[row]
        off = rng.random(len(a))
        u = ((rank + off[row]) / np.maximum(gcount[row], 1)).astype(np.float32)

        # pick a block for every dot, weighted by 2020 housing units within the block group
        bgpos = pos.reindex(a.index.to_numpy()).to_numpy()
        has = ~np.isnan(bgpos)
        lost = ~has[row]
        missing += int(lost.sum())
        keep = ~lost
        row, b, u = row[keep], b[keep], u[keep]
        p = bgpos[row].astype(np.int64)
        s, c = bstart[p], bcount[p]
        hw = np.add.reduceat(housing, bstart)
        lw = np.add.reduceat(aland, bstart)
        # per-block weight: housing if the BG has any 2020 housing, else land area, else uniform
        bg_of_block = np.repeat(np.arange(len(ukeys)), bcount)
        w = np.where(hw[bg_of_block] > 0, housing, np.where(lw[bg_of_block] > 0, aland, 1.0))
        cw = np.cumsum(w)
        base = np.where(s > 0, cw[s - 1], 0.0)
        tot = cw[s + c - 1] - base
        r = base + rng.random(len(row)) * tot
        blk = np.searchsorted(cw, r, side='right')
        blk = np.clip(blk, s, s + c - 1)
        # skip zero-weight blocks that floating point may land on
        zero = w[blk] <= 0
        if zero.any():
            for i in np.flatnonzero(zero):
                j = blk[i]
                while j > s[i] and w[j] <= 0: j -= 1
                while j < s[i] + c[i] - 1 and w[j] <= 0: j += 1
                blk[i] = j

        x = np.empty(len(blk)); y = np.empty(len(blk))
        todo = np.arange(len(blk))
        if B is not None:
            # onto a building of the block, by its homes and fit with this tenure
            cwb, wst, wen, btot = building_weights(B, tenure, len(geoms))
            on = btot[blk] > 0
            i = np.flatnonzero(on)
            base_b = np.where(wst[blk[i]] > 0, cwb[np.maximum(wst[blk[i]] - 1, 0)], 0.0)
            j = np.searchsorted(cwb, base_b + rng.random(len(i)) * btot[blk[i]], side='right')
            j = np.clip(j, wst[blk[i]], wen[blk[i]] - 1)
            ang = rng.random(len(i)) * 2 * np.pi
            rad = B['radius'][j] * np.sqrt(rng.random(len(i)))
            y[i] = B['lat'][j] + rad * np.sin(ang) / M_PER_DEG
            x[i] = B['lon'][j] + rad * np.cos(ang) / (M_PER_DEG * np.cos(np.radians(B['lat'][j])))
            todo = np.flatnonzero(~on)
            on_building += int(on.sum())
        # elsewhere a uniform point in the block polygon, by rejection sampling inside its bounding box
        for _ in range(60):
            if not len(todo): break
            bb = bounds[blk[todo]]
            cx = bb[:, 0] + rng.random(len(todo)) * (bb[:, 2] - bb[:, 0])
            cy = bb[:, 1] + rng.random(len(todo)) * (bb[:, 3] - bb[:, 1])
            ok = shapely.contains_xy(geoms[blk[todo]], cx, cy)
            x[todo[ok]] = cx[ok]; y[todo[ok]] = cy[ok]
            todo = todo[~ok]
        if len(todo):   # slivers: fall back to a point guaranteed inside
            pts = shapely.point_on_surface(geoms[blk[todo]])
            x[todo] = shapely.get_x(pts); y[todo] = shapely.get_y(pts)
        stats[tenure] = dict(units=float(C.sum()), dots=int(len(row)), fallback=int(len(todo)))

        X, Y = mercator24(x, y)
        xs.append(X); ys.append(Y); ts.append(np.full(len(row), tenure, np.uint8)); bs.append(b)
        ms.append(med[row]); us.append(u); gs.append(gidx_all.reindex(a.index[row]).to_numpy().astype(np.int32))

    np.savez(os.path.join(DOTS, st + '.npz'), x=np.concatenate(xs), y=np.concatenate(ys), t=np.concatenate(ts),
             b=np.concatenate(bs), m=np.concatenate(ms), u=np.concatenate(us), g=np.concatenate(gs))
    msg = (f'{st}: {len(a)} bgs, {len(geoms)} blocks, value {stats[0]["units"]:,.0f} homes -> {stats[0]["dots"]:,} dots, '
           f'rent {stats[1]["units"]:,.0f} -> {stats[1]["dots"]:,} dots, lost {missing}, '
           f'fallback {stats[0]["fallback"] + stats[1]["fallback"]}, on buildings '
           f'{on_building / max(stats[0]["dots"] + stats[1]["dots"], 1):.1%}, {time.time() - t0:.0f}s')
    print(msg, flush=True)
    return st, stats, missing

def main():
    os.makedirs(DOTS, exist_ok=True)
    states = sys.argv[1:] or STATES
    states = sorted(states, key=lambda s: -os.path.getsize(os.path.join(WORK, 'blocks', s + '.zip')))
    with mp.Pool(int(os.environ.get('PROCS', 4)), maxtasksperchild=1) as pool:
        res = pool.map(process, states, chunksize=1)
    tot = {0: [0, 0], 1: [0, 0]}
    for st, stats, missing in res:
        for t in (0, 1):
            tot[t][0] += stats[t]['units']; tot[t][1] += stats[t]['dots']
    for t in (0, 1):
        print(['value', 'rent'][t], f'{tot[t][0]:,.0f} homes, {tot[t][1]:,} dots, ratio {tot[t][0] / max(tot[t][1], 1):.3f}')

if __name__ == '__main__':
    main()
