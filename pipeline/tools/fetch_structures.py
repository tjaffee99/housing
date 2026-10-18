"""Download FEMA/ORNL USA Structures (every US building over 450 sq ft, with occupancy class) one state
at a time and keep what dot placement needs: each building's centroid, type, footprint area and
height. Non-residential buildings are kept too: in dense cities the source often merges a row of
houses into one outline and labels it by a school or shop in it, and mixed-use apartment buildings
are often labeled commercial. The downloads are deleted as they are processed.

Run from pipeline/:   python3 tools/fetch_structures.py [state FIPS ...]
Output  work/structs/<FIPS>.npz   lon, lat (float64), kind (0 single-family, 1 multi-family,
                                  2 manufactured, 3 other residential, 4 unclassified, 5 non-residential),
                                  sqm (footprint, m^2), height (m, NaN if unknown); and the outlines
                                  of buildings of BIG m^2 or more (many are whole rows of attached
                                  houses, so a dot needs the real shape, not a circle round the
                                  center): big (row index), coords, ring_off, poly_off (shapely
                                  ragged-array polygons, lon/lat, simplified to ~50 cm)
"""
import os, re, sys, glob, time, shutil, zipfile, urllib.parse, urllib.request
import numpy as np, pandas as pd, pyogrio, shapely
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import WORK, STATES

BUCKET = 'https://fema-femadata.s3.amazonaws.com/'
PREFIX = 'Partners/ORNL/USA_Structures/'
OUT = os.path.join(WORK, 'structs')
BIG = 400            # m^2: footprints at least this large keep their outline
VERSION = 4
NAMES = {'01': 'Alabama', '02': 'Alaska', '04': 'Arizona', '05': 'Arkansas', '06': 'California', '08': 'Colorado',
         '09': 'Connecticut', '10': 'Delaware', '11': 'District of Columbia', '12': 'Florida', '13': 'Georgia',
         '15': 'Hawaii', '16': 'Idaho', '17': 'Illinois', '18': 'Indiana', '19': 'Iowa', '20': 'Kansas',
         '21': 'Kentucky', '22': 'Louisiana', '23': 'Maine', '24': 'Maryland', '25': 'Massachusetts',
         '26': 'Michigan', '27': 'Minnesota', '28': 'Mississippi', '29': 'Missouri', '30': 'Montana',
         '31': 'Nebraska', '32': 'Nevada', '33': 'New Hampshire', '34': 'New Jersey', '35': 'New Mexico',
         '36': 'New York', '37': 'North Carolina', '38': 'North Dakota', '39': 'Ohio', '40': 'Oklahoma',
         '41': 'Oregon', '42': 'Pennsylvania', '44': 'Rhode Island', '45': 'South Carolina', '46': 'South Dakota',
         '47': 'Tennessee', '48': 'Texas', '49': 'Utah', '50': 'Vermont', '51': 'Virginia', '53': 'Washington',
         '54': 'West Virginia', '55': 'Wisconsin', '56': 'Wyoming', '72': 'Puerto Rico'}


def listing():
    with urllib.request.urlopen(f'{BUCKET}?list-type=2&prefix={urllib.parse.quote(PREFIX)}', timeout=120) as r:
        x = r.read().decode()
    rows = re.findall(r'<Key>([^<]+)</Key><LastModified>([^<]+)</LastModified><ETag>[^<]*</ETag><Size>(\d+)</Size>', x)
    return [(k, d, int(s)) for k, d, s in rows if int(s) > 0 and k.endswith('.zip')]


def pick(rows, name):
    """The state's newest full deliverable: the latest 2023 release (2024 re-releases cover parts of
    some states only), else the latest of any year."""
    mine = [r for r in rows if r[0].startswith(f'{PREFIX}{name}/')]
    y23 = [r for r in mine if r[1].startswith('2023')]
    return max(y23 or mine, key=lambda r: r[1])


def kind_of(occ, prim):
    prim = np.char.lower(prim.astype(str))
    k = np.full(len(occ), 5, np.uint8)
    res = occ == 'Residential'
    k[res] = 3
    k[res & (np.char.find(prim, 'single') >= 0)] = 0
    k[res & (np.char.find(prim, 'multi') >= 0)] = 1
    k[res & ((np.char.find(prim, 'manufactured') >= 0) | (np.char.find(prim, 'mobile') >= 0))] = 2
    k[occ == 'Unclassified'] = 4
    return k


def process(st, rows):
    dst = os.path.join(OUT, st + '.npz')
    if os.path.exists(dst) and int(np.load(dst).get('version', 0)) >= VERSION: return f'{st}: cached'
    key, date, size = pick(rows, NAMES[st])
    tmp = os.path.join(OUT, 'tmp_' + st); os.makedirs(tmp, exist_ok=True)
    zpath = os.path.join(tmp, 'd.zip')
    t0 = time.time()
    for attempt in range(4):
        try:
            with urllib.request.urlopen(BUCKET + urllib.parse.quote(key), timeout=600) as r, open(zpath, 'wb') as f:
                shutil.copyfileobj(r, f, 1 << 22)
            if os.path.getsize(zpath) != size or not zipfile.is_zipfile(zpath):
                raise IOError(f'got {os.path.getsize(zpath):,} of {size:,} bytes')
            break
        except Exception as e:
            print(f'{st}: retry {attempt + 1}: {e}', flush=True); time.sleep(10 * 2 ** attempt)
    else:
        raise SystemExit(f'{st}: download failed')
    t1 = time.time()
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    os.remove(zpath)
    gdb = [g for g in glob.glob(os.path.join(tmp, '**', '*.gdb'), recursive=True) if '__MACOSX' not in g][0]
    cols = ['OCC_CLS', 'PRIM_OCC', 'SQMETERS', 'HEIGHT', 'LONGITUDE', 'LATITUDE']
    meta, fids, _, arrs = pyogrio.raw.read(gdb, read_geometry=False, columns=cols, return_fids=True)
    d = dict(zip(meta['fields'], arrs))          # (fields come back in the file's order)
    k = kind_of(np.asarray(d['OCC_CLS'], dtype=object).astype(str), np.asarray(d['PRIM_OCC'], dtype=object))
    keep = np.isfinite(np.asarray(d['LONGITUDE'], float)) & np.isfinite(np.asarray(d['LATITUDE'], float))
    # outlines of the big buildings: largest part, simplified to ~50 cm
    _, gfids, wkb, _ = pyogrio.raw.read(gdb, columns=['SQMETERS'], return_fids=True,   # (filtered fields must be read)
                                        where=f"SQMETERS >= {BIG}")
    parts, owner = shapely.get_parts(shapely.from_wkb(wkb), return_index=True)
    best = pd.Series(shapely.area(parts)).groupby(owner).idxmax().to_numpy()
    polys = shapely.simplify(parts[best], 5e-6)
    gfids = gfids[np.unique(owner)]
    ok = ~shapely.is_empty(polys) & (shapely.get_type_id(polys) == 3)
    kept_fids = fids[keep]
    order = np.argsort(kept_fids)
    pos = np.searchsorted(kept_fids[order], gfids[ok])
    pos = np.clip(pos, 0, len(order) - 1)
    match = kept_fids[order][pos] == gfids[ok]
    big = order[pos[match]].astype(np.int32)
    _, coords, (ring_off, poly_off) = shapely.to_ragged_array(polys[ok][match])
    np.savez(dst, lon=np.asarray(d['LONGITUDE'], float)[keep], lat=np.asarray(d['LATITUDE'], float)[keep], kind=k[keep],
             sqm=np.asarray(d['SQMETERS'], np.float32)[keep], height=np.asarray(d['HEIGHT'], np.float32)[keep],
             big=big, coords=coords, ring_off=ring_off.astype(np.int64), poly_off=poly_off.astype(np.int64),
             version=np.int8(VERSION))
    shutil.rmtree(tmp)
    counts = np.bincount(k[keep], minlength=6)
    return (f'{st}: {os.path.basename(key)} ({size / 1e9:.1f} GB, {date[:10]}) {keep.sum():,} buildings kept '
            f'[single {counts[0]:,} multi {counts[1]:,} manuf {counts[2]:,} other {counts[3]:,} unclassified {counts[4]:,} non-residential {counts[5]:,}; '
            f'{len(big):,} outlines] '
            f'download {t1 - t0:.0f}s, read {time.time() - t1:.0f}s')


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = listing()
    states = sys.argv[1:] or STATES
    # biggest first, one at a time: an extracted state can take 2x its download on disk
    states = sorted(states, key=lambda s: -pick(rows, NAMES[s])[2])
    for st in states:
        print(process(st, rows), flush=True)


if __name__ == '__main__':
    main()
