"""Pack or unpack the base data a monthly refresh needs (everything 04_prices.py onward reads that
doesn't change month to month), so the refresh can run without the census blocks or HMDA.

    python3 tools/kit.py pack ../kit      # after a full build: write the kit (files under 90 MB)
    python3 tools/kit.py unpack ../kit    # in the refresh job: restore work/base, work/acs.pkl, work/hud

The kit lives on the repository's `base` branch (see .github/workflows/refresh.yml).
"""
import os, sys, json, glob, gzip, shutil, pickle, hashlib, datetime
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import WORK, STATES, ACS_YEAR, HUD_FY

BASE = os.path.join(WORK, 'base')
CHUNK = 80_000_000
DOT_FIELDS = ('x', 'y', 't', 'u', 'g', 'z', 'ref', 'q', 'nc')
# ACS columns the refresh still reads (04_prices.py, 06_areas.py)
ACS_COLS = ['owners', 'renters', 'households', 'units', 'vacant', 'rent_burden', 'med_income', 'synth', 'med_tax',
            'med_value', 'med_value_moe', 'med_rent', 'med_rent_moe'] + [f'rb{b}' for b in range(6)]


def pack(kit):
    os.makedirs(kit, exist_ok=True)
    for f in glob.glob(os.path.join(kit, '*')):
        if os.path.isfile(f): os.remove(f)
    files = []
    # dots, grouped by state into compressed files of about CHUNK bytes
    group, size, n = {}, 0, 0
    def flush():
        nonlocal group, size, n
        if not group: return
        fn = f'dots-{n}.npz'
        np.savez_compressed(os.path.join(kit, fn), **group)
        files.append(fn); n += 1; group, size = {}, 0
    for st in STATES:
        d = np.load(os.path.join(BASE, st + '.npz'))
        arrs = {k: d[k] for k in DOT_FIELDS}
        arrs['z'] = np.where(arrs['z'] < 0, 65535, arrs['z']).astype(np.uint16)
        est = sum(a.nbytes for a in arrs.values()) * 0.45
        if size and size + est > CHUNK: flush()
        group.update({f'{st}_{k}': v for k, v in arrs.items()}); size += est
    flush()
    P = np.load(os.path.join(BASE, 'points.npz'))
    np.savez_compressed(os.path.join(kit, 'points.npz'), **{k: P[k] for k in P.files if k != 'mref'})
    files.append('points.npz')
    acs = pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))
    slim = {lvl: d[[c for c in ACS_COLS if c in d]] for lvl, d in acs.items()}
    with gzip.open(os.path.join(kit, 'acs.pkl.gz'), 'wb') as f: pickle.dump(slim, f, protocol=5)
    shutil.copy(os.path.join(BASE, 'meta.pkl'), os.path.join(kit, 'meta.pkl'))
    hud = [f'fy{HUD_FY}_safmrs.xlsx', f'fy{HUD_FY}_fmrs.xlsx']
    for fn in hud:
        shutil.copy(os.path.join(WORK, 'hud', fn), os.path.join(kit, fn))
    files += ['acs.pkl.gz', 'meta.pkl'] + hud
    sums = {fn: hashlib.sha256(open(os.path.join(kit, fn), 'rb').read()).hexdigest() for fn in files}
    json.dump({'built': datetime.date.today().isoformat(), 'acs': ACS_YEAR, 'files': sums}, open(os.path.join(kit, 'kit.json'), 'w'), indent=1)
    for fn in files:
        sz = os.path.getsize(os.path.join(kit, fn))
        assert sz < 95_000_000, f'{fn} is {sz / 1e6:.0f} MB, over GitHub\'s per-file limit'
        print(f'{sz / 1e6:7.1f} MB  {fn}')


def unpack(kit):
    info = json.load(open(os.path.join(kit, 'kit.json')))
    for fn, h in info['files'].items():
        if hashlib.sha256(open(os.path.join(kit, fn), 'rb').read()).hexdigest() != h:
            raise SystemExit(f'kit file {fn} does not match its checksum')
    os.makedirs(BASE, exist_ok=True)
    for fn in sorted(glob.glob(os.path.join(kit, 'dots-*.npz'))):
        d = np.load(fn)
        for st in sorted({k.split('_')[0] for k in d.files}):
            arrs = {k: d[f'{st}_{k}'] for k in DOT_FIELDS}
            z = arrs['z'].astype(np.int32)
            z[z == 65535] = -1
            arrs['z'] = z
            np.savez(os.path.join(BASE, st + '.npz'), **arrs)
    shutil.copy(os.path.join(kit, 'points.npz'), os.path.join(BASE, 'points.npz'))
    shutil.copy(os.path.join(kit, 'meta.pkl'), os.path.join(BASE, 'meta.pkl'))
    with gzip.open(os.path.join(kit, 'acs.pkl.gz'), 'rb') as f: acs = pickle.load(f)
    pickle.dump(acs, open(os.path.join(WORK, 'acs.pkl'), 'wb'))
    os.makedirs(os.path.join(WORK, 'hud'), exist_ok=True)
    for f in glob.glob(os.path.join(kit, 'fy*.xlsx')):
        shutil.copy(f, os.path.join(WORK, 'hud', os.path.basename(f)))
    print(f'kit from {info["built"]} unpacked: {len(glob.glob(os.path.join(BASE, "[0-9]*.npz")))} states')


if __name__ == '__main__':
    {'pack': pack, 'unpack': unpack}[sys.argv[1]](sys.argv[2])
