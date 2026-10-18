"""Encode the dots as Mapbox Vector Tiles in PMTiles archives: one archive set per zoom tier and mode,
so the app only downloads what is on screen.

  v  owner-occupied homes by current value                     k = value class
  p  renter-occupied homes by the rent they pay                k = monthly class (NO_CASH_CLASS: no cash rent)
  m  renter-occupied homes by market rent                      k = monthly class
  c  every occupied home by monthly cost (buying vs renting)   k = monthly class, +16 for renters

Dots that share a class `k` are packed into MultiPoint features of at most CHUNK points (Hilbert-ordered,
so each chunk is spatially compact); chunks are drawn in random order so no class always paints on top.

Inputs  work/base/*.npz (positions, nesting priority), work/now/*.npz (classes)
Output  data/dots/<tier><v|p|m|c>[-<shard>].pmtiles, work/tiers.json (read by 09_manifest.py)
"""
import os, json, glob, gzip, time
import numpy as np
from pmtiles.writer import Writer
from pmtiles.tile import zxy_to_tileid, TileType, Compression
from common import WORK, OUT, TIERS, FINEST, EXTENT, SHARD_BYTES, STATES

CHUNK = 32
RENTER = 16      # class offset for renters in the monthly-cost archive

def varint(n):
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7f) | 0x80); n >>= 7
    out.append(n)
    return bytes(out)

def varint_array(v):
    """Encode non-negative int64 array as concatenated varints; return bytes and per-value end offsets."""
    v = v.astype(np.int64)
    nb = 1 + (v >= 1 << 7) + (v >= 1 << 14) + (v >= 1 << 21)
    cols = np.stack([(v >> (7 * k)) & 0x7f for k in range(4)], 1)
    cont = np.arange(4)[None, :] < (nb[:, None] - 1)
    cols = cols | (cont * 0x80)
    data = cols[np.arange(4)[None, :] < nb[:, None]].astype(np.uint8).tobytes()
    return data, np.cumsum(nb)

def hilbert(x, y, order):
    """Vectorized Hilbert index of (x, y) on a 2^order grid."""
    x = x.astype(np.int64).copy(); y = y.astype(np.int64).copy()
    d = np.zeros_like(x)
    s = 1 << (order - 1)
    while s > 0:
        rx = (x & s) > 0
        ry = (y & s) > 0
        d += s * s * ((3 * rx) ^ ry)
        # rotate
        m = ~ry
        flip = m & rx
        x = np.where(flip, s - 1 - x, x); y = np.where(flip, s - 1 - y, y)
        x, y = np.where(m, y, x), np.where(m, x, y)
        s >>= 1
    return d

def tileids(z, tx, ty):
    base = sum(4 ** i for i in range(z))
    n = 1 << z
    # PMTiles tile ids: Hilbert index within zoom, matching pmtiles.tile.zxy_to_tileid
    return base + hilbert(tx, ty, z) if z > 0 else np.zeros_like(tx, dtype=np.int64)

def load_dots():
    parts = {k: [] for k in ('x', 'y', 't', 'u', 'kv', 'kp', 'km', 'kc')}
    for st in STATES:
        d = np.load(os.path.join(WORK, 'base', st + '.npz'))
        n = np.load(os.path.join(WORK, 'now', st + '.npz'))
        for k in parts: parts[k].append(d[k] if k in d else n[k])
    return {k: np.concatenate(v) for k, v in parts.items()}

def encode_zoom(D, z, rng, lname):
    """Return list of (tileid, x, gzipped tile bytes) for all tiles at zoom z; one layer `lname`,
    features grouped by class D['k']."""
    shift = 12 - z
    ux = (D['x'] >> shift).astype(np.int64); uy = (D['y'] >> shift).astype(np.int64)
    tx, ty = ux >> 12, uy >> 12
    lx, ly = ux & (EXTENT - 1), uy & (EXTENT - 1)
    tid = tileids(z, tx, ty)
    hl = hilbert(lx, ly, 12)
    b = D['k'].astype(np.int64)
    order = np.lexsort((hl, b, tid))
    tid, tx, lx, ly, b = tid[order], tx[order], lx[order], ly[order], b[order]
    n = len(tid)
    # feature boundaries: new (tile, class) group, or every CHUNK points within a group
    newgrp = np.ones(n, bool)
    newgrp[1:] = (tid[1:] != tid[:-1]) | (b[1:] != b[:-1])
    gid = np.cumsum(newgrp) - 1
    gstart = np.flatnonzero(newgrp)
    rank = np.arange(n) - gstart[gid]
    fstart_mask = newgrp | (rank % CHUNK == 0)
    fstarts = np.flatnonzero(fstart_mask)
    fends = np.append(fstarts[1:], n)
    # geometry deltas, cursor reset at each feature
    px, py = np.empty(n, np.int64), np.empty(n, np.int64)
    px[0], py[0] = lx[0], ly[0]
    px[1:] = lx[1:] - lx[:-1]; py[1:] = ly[1:] - ly[:-1]
    px[fstart_mask], py[fstart_mask] = lx[fstart_mask], ly[fstart_mask]
    inter = np.empty(2 * n, np.int64); inter[0::2] = px; inter[1::2] = py
    zz = (inter << 1) ^ (inter >> 63)
    data, ends = varint_array(zz)
    starts_b = np.concatenate([[0], ends])           # byte offset of value i
    out = []
    tile_starts = np.flatnonzero(np.r_[True, tid[1:] != tid[:-1]])
    tile_ends = np.append(tile_starts[1:], n)
    fptr = 0
    for ts, te in zip(tile_starts, tile_ends):
        f0 = fptr
        while fptr < len(fstarts) and fstarts[fptr] < te: fptr += 1
        fl = list(range(f0, fptr))
        vals = sorted({int(b[fstarts[f]]) for f in fl})
        vidx = {v: i for i, v in enumerate(vals)}
        rng.shuffle(fl)
        body = bytearray()
        for f in fl:
            s, e = fstarts[f], fends[f]
            geom = varint(((e - s) << 3) | 1) + data[starts_b[2 * s]:starts_b[2 * e]]
            tags = b'\x00' + varint(vidx[int(b[s])])
            feat = b'\x12' + varint(len(tags)) + tags + b'\x18\x01\x22' + varint(len(geom)) + geom
            body += b'\x12' + varint(len(feat)) + feat
        layer = (b'\x78\x02\x0a' + varint(len(lname)) + lname + bytes(body) + b'\x1a\x01k'
                 + b''.join(b'\x22' + varint(len(vv)) + vv for vv in (b'\x28' + varint(v) for v in vals))
                 + b'\x28' + varint(EXTENT))
        out.append((int(tid[ts]), int(tx[ts]), gzip.compress(b'\x1a' + varint(len(layer)) + layer, compresslevel=9, mtime=0)))
    return out


def write_archive(path, tiles, z0, z1, meta):
    tiles = sorted(tiles, key=lambda r: r[0])
    with open(path, 'wb') as f:
        w = Writer(f)
        for tid, _, data in tiles:
            w.write_tile(tid, data)
        w.finalize({
            'tile_type': TileType.MVT, 'tile_compression': Compression.GZIP,
            'min_lon_e7': int(-179.2 * 1e7), 'min_lat_e7': int(17.6 * 1e7),
            'max_lon_e7': int(-64.5 * 1e7), 'max_lat_e7': int(71.5 * 1e7),
            'center_zoom': 4, 'center_lon_e7': int(-98.5 * 1e7), 'center_lat_e7': int(39.5 * 1e7),
        }, meta)

# mode letter -> (tenure filter: 0 owners, 1 renters, None everyone; class array)
MODES = (('v', 0, 'kv'), ('p', 1, 'kp'), ('m', 1, 'km'), ('c', None, 'kc'))

def write_set(tier, letter, tiles, meta):
    """Write one archive, or several tile-column shards if it is bigger than SHARD_BYTES."""
    total = sum(len(r[2]) for r in tiles)
    zmax = max(tier['zooms'])
    base = f'{tier["name"]}{letter}'
    nshard = max(1, -(-total // SHARD_BYTES))
    if nshard == 1 or len(tier['zooms']) > 1:
        write_archive(os.path.join(OUT, 'dots', base + '.pmtiles'), [(a, b_, c) for a, b_, c, _ in tiles], min(tier['zooms']), zmax, meta)
        return [{'file': base + '.pmtiles', 'x0': 0, 'x1': (1 << zmax) - 1}], total
    cols = {}
    for r in tiles: cols[r[1]] = cols.get(r[1], 0) + len(r[2])
    acc, cuts, target = 0, [], total / nshard
    for x in sorted(cols):
        acc += cols[x]
        if acc >= target * (len(cuts) + 1) and len(cuts) < nshard - 1: cuts.append(x)
    bounds = [0] + [c + 1 for c in cuts] + [1 << zmax]
    shards = []
    for i in range(len(bounds) - 1):
        x0, x1 = bounds[i], bounds[i + 1] - 1
        fn = f'{base}-{i}.pmtiles'
        write_archive(os.path.join(OUT, 'dots', fn), [(a, b_, c) for a, b_, c, _ in tiles if x0 <= b_ <= x1], zmax, zmax, meta)
        shards.append({'file': fn, 'x0': x0, 'x1': x1})
    return shards, total

def main():
    rng = np.random.default_rng(7)
    D = load_dots()
    print(f'{len(D["x"]):,} dots loaded')
    os.makedirs(os.path.join(OUT, 'dots'), exist_ok=True)
    for f in glob.glob(os.path.join(OUT, 'dots', '*.pmtiles')): os.remove(f)
    tiers = []
    for tier in TIERS:
        t0 = time.time()
        keep = D['u'] < FINEST / tier['ratio']
        entry = {'name': tier['name'], 'ratio': tier['ratio'], 'minzoom': min(tier['zooms']), 'maxzoom': max(tier['zooms']),
                 'show': list(tier['show']), 'dots': {}, 'files': {}}
        for letter, tenure, kcol in MODES:
            sel = keep if tenure is None else keep & (D['t'] == tenure)
            k = D[kcol][sel].astype(np.int64)
            if tenure is None:
                k = k + RENTER * D['t'][sel]
            sub = {'x': D['x'][sel], 'y': D['y'][sel], 'k': k}
            tiles = []
            for z in tier['zooms']:
                tiles += [(tid, x, data, z) for tid, x, data in encode_zoom(sub, z, rng, letter.encode())]
            meta = {'name': tier['name'] + letter, 'vector_layers': [{'id': letter, 'fields': {'k': 'Number'}}], 'ratio': tier['ratio']}
            shards, total = write_set(tier, letter, tiles, meta)
            entry['dots'][letter] = int(sel.sum())
            entry['files'][letter] = shards
            sizes = [os.path.getsize(os.path.join(OUT, 'dots', s['file'])) for s in shards]
            print(f'{tier["name"]}{letter}: ratio {tier["ratio"]}, {sel.sum():,} dots, {len(tiles):,} tiles, '
                  f'files {[f"{s / 1e6:.1f}MB" for s in sizes]}', flush=True)
        print(f'  {time.time() - t0:.0f}s')
        tiers.append(entry)
    json.dump(tiers, open(os.path.join(WORK, 'tiers.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()
