"""Read the ACS 5-year table-based summary files into one table per geography level.

Inputs  work/acs/<table>.dat   (www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/)
        work/blocks/<SS>.zip   (only for counties whose block groups don't add up; see synthesize_missing)
Output  work/acs.pkl           dict level -> DataFrame indexed by GEOID (state/county/tract/bg/us)
"""
import os, pickle
import numpy as np, pandas as pd, pyogrio
from common import WORK, STATES

LEVELS = {'0100000US': 'us', '0400000US': 'state', '0500000US': 'county', '1400000US': 'tract', '1500000US': 'bg'}

def read(table, cols):
    df = pd.read_csv(os.path.join(WORK, 'acs', table + '.dat'), sep='|', dtype=str, usecols=['GEO_ID'] + cols)
    df = df[df.GEO_ID.str[:9].isin(list(LEVELS))].copy()
    df['level'] = df.GEO_ID.str[:9].map(LEVELS)
    df['geoid'] = df.GEO_ID.str[9:]
    df = df.set_index(['level', 'geoid']).drop(columns='GEO_ID')
    out = df.apply(pd.to_numeric, errors='coerce').astype('float64')
    return out

def E(t, i): return f'{t}_E{i:03d}'
def M(t, i): return f'{t}_M{i:03d}'

MEDIANS = ['med_value', 'med_rent', 'med_income', 'rent_burden']

def synthesize_missing(out):
    """Fill block groups the ACS release omits (it has no rows for 39 Suffolk County, NY block groups,
    though their 2020 blocks hold 17,000 homes). Each such block group gets a share of its county's
    residual (county total minus the published block groups), split by 2020 housing units. Medians
    are left missing, so later steps estimate them from the brackets; `synth` = 1 marks the rows."""
    bg, tract, county = out['bg'], out['tract'], out['county']
    counts = [c for c in bg.columns if c not in MEDIANS and not c.endswith('_moe')]
    resid = county[counts] - bg[counts].groupby(bg.index.str[:5]).sum().reindex(county.index).fillna(0)
    resid = resid.clip(lower=0)
    short = resid.index[(resid.owners + resid.renters) > 0]
    new_bg, new_tract = [], []
    for cid in short:
        blocks = pyogrio.read_dataframe(os.path.join(WORK, 'blocks', cid[:2] + '.zip'), read_geometry=False,
                                        columns=['GEOID20', 'HOUSING20', 'COUNTYFP20'], where=f"COUNTYFP20 = '{cid[2:]}'")
        hu = blocks.groupby(blocks.GEOID20.str[:12]).HOUSING20.sum()
        missing = hu[~hu.index.isin(bg.index) & (hu > 0)]
        if missing.empty:
            print(f'warning: county {cid} is short {resid.loc[cid, "owners"]:.0f} owners / '
                  f'{resid.loc[cid, "renters"]:.0f} renters but has no unpublished block groups')
            continue
        share = missing / missing.sum()
        rows = pd.DataFrame(np.outer(share.to_numpy(), resid.loc[cid].to_numpy()), index=share.index, columns=counts)
        new_bg.append(rows)
        # a tract missing from ACS = all its block groups, published and synthesized;
        # synth = 1 if every block group is synthesized, 2 if only some are
        tids = set(rows.index.str[:11]) - set(tract.index)
        pub = bg[bg.index.str[:11].isin(tids)][counts]
        allbg = pd.concat([pub, rows])
        t = allbg.groupby(allbg.index.str[:11]).sum()
        t['synth'] = np.where(t.index.isin(set(pub.index.str[:11])), 2, 1)
        new_tract.append(t)
        print(f'county {cid}: {len(rows)} unpublished block groups get the residual '
              f'{resid.loc[cid, "owners"]:,.0f} owner / {resid.loc[cid, "renters"]:,.0f} renter homes')
    for lvl, new in (('bg', new_bg), ('tract', new_tract)):
        out[lvl]['synth'] = 0
        if new:
            add = pd.concat(new)
            flag = add['synth'] if 'synth' in add else 1
            add = add.reindex(columns=out[lvl].columns)
            add['synth'] = flag
            out[lvl] = pd.concat([out[lvl], add]).sort_index()

def main():
    parts = []
    val = read('b25075', [E('B25075', i) for i in range(1, 28)])
    val.columns = ['val_total'] + [f'v{i}' for i in range(26)]
    parts.append(val)
    rent = read('b25063', [E('B25063', i) for i in range(1, 28)])
    # 001 total, 002 with cash rent, 003..026 cash-rent bins, 027 no cash rent
    rent.columns = ['rent_total', 'rent_cash'] + [f'r{i}' for i in range(24)] + ['r24']
    parts.append(rent)
    for t, name in [('b25077', 'med_value'), ('b25064', 'med_rent'), ('b19013', 'med_income'), ('b25071', 'rent_burden')]:
        d = read(t, [E(t.upper(), 1), M(t.upper(), 1)])
        d.columns = [name, name + '_moe']
        parts.append(d)
    ten = read('b25003', [E('B25003', i) for i in (1, 2, 3)]); ten.columns = ['households', 'owners', 'renters']; parts.append(ten)
    hu = read('b25001', [E('B25001', 1)]); hu.columns = ['units']; parts.append(hu)
    occ = read('b25002', [E('B25002', 3)]); occ.columns = ['vacant']; parts.append(occ)
    df = pd.concat(parts, axis=1)
    # ACS annotation codes (-666666666 etc.) are stored as large negative numbers: treat as missing.
    df = df.mask(df < 0)
    out = {lvl: df.xs(lvl, level='level').sort_index() for lvl in LEVELS.values()}
    out['state'] = out['state'][out['state'].index.isin(STATES)]
    for lvl in ('county', 'tract', 'bg'):
        out[lvl] = out[lvl][out[lvl].index.str[:2].isin(STATES)]
    synthesize_missing(out)
    for lvl, d in out.items():
        print(f'{lvl:7s} {len(d):7d} rows   owners={d.owners.sum():,.0f}  renters={d.renters.sum():,.0f}  '
              f'val_total={d.val_total.sum():,.0f}  rent_total={d.rent_total.sum():,.0f}')
    with open(os.path.join(WORK, 'acs.pkl'), 'wb') as f:
        pickle.dump(out, f)

if __name__ == '__main__':
    main()
