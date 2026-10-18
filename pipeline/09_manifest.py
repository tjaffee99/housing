"""Write data/manifest.json, the index the web app loads first."""
import os, json, pickle, datetime
import numpy as np
from common import WORK, OUT, VALUE_CLASSES, MONTHLY_CLASSES, FINEST, ACS_YEAR, DOWN, TERM_MONTHS, INSURANCE, CA_MIN_TAX


def short(v):
    if v >= 1e6: return f'{v / 1e6:g}M'
    if v >= 1e3: return f'{v / 1e3:g}K'
    return f'{v:g}'


def value_labels(e):
    out = ['Under $' + short(e[1])]
    for lo, hi in zip(e[1:-1], e[2:]):
        a, b = short(lo), short(hi)
        if a[-1] == b[-1]: a = a[:-1]            # $100–150K, $1–1.5M
        out.append(f'${a}–{b}')
    return out + ['$' + short(e[-1]) + '+']


def monthly_labels(e):
    def m(v): return f'{v / 1e3:g}K' if v >= 10_000 else f'{v:,}'
    out = ['Under $' + m(e[1])]
    for lo, hi in zip(e[1:-1], e[2:]):
        a, b = m(lo), m(hi)
        if a.endswith('K') and b.endswith('K'): a = a[:-1]
        out.append(f'${a}–{b}')
    return out + ['$' + m(e[-1]) + '+']


def num(x):
    return None if x is None or x != x else int(round(x))


def main():
    tiers = json.load(open(os.path.join(WORK, 'tiers.json')))
    files = json.load(open(os.path.join(WORK, 'areas.json')))
    fields = json.load(open(os.path.join(WORK, 'fields.json')))['fields']
    vintage = json.load(open(os.path.join(WORK, 'now', 'vintage.json')))
    stats = pickle.load(open(os.path.join(WORK, 'now', 'stats.pkl'), 'rb'))
    us = stats['us'].iloc[0]
    ints = lambda a: [int(round(x)) for x in a]
    # ZIP code size view: share of occupied homes in ZIPs whose typical N-bedroom price falls in each class
    z, sz = stats['zcta'], stats['sizes']
    homes = (z.owners + z.renters).reindex(sz.index).fillna(0).to_numpy()
    sizes = {}
    for col in [f'v{b}' for b in range(1, 6)] + [f'm{b}' for b in range(5)] + [f'p{b}' for b in range(5)] + [f'c{b}' for b in range(1, 6)]:
        edges = VALUE_CLASSES if col[0] == 'v' else MONTHLY_CLASSES
        v = sz[col].to_numpy(float)
        ok = np.isfinite(v)
        k = np.clip(np.searchsorted(edges, v[ok], side='right') - 1, 0, len(edges) - 1)
        sizes[col] = ints(np.bincount(k, weights=homes[ok], minlength=len(edges)))
    ml = monthly_labels(MONTHLY_CLASSES)
    manifest = {
        'built': datetime.date.today().isoformat(),
        'acs': f'{ACS_YEAR - 4}–{ACS_YEAR}',
        'vintage': {**vintage, 'term_years': TERM_MONTHS // 12},
        'finest': FINEST,
        'tiers': tiers,
        'areas': files,
        'classes': {
            'v': {'edges': VALUE_CLASSES, 'labels': value_labels(VALUE_CLASSES), 'us': ints(us.hv)},
            'p': {'edges': MONTHLY_CLASSES, 'labels': ml + ['No cash rent'], 'us': ints(us.hp)},
            'm': {'edges': MONTHLY_CLASSES, 'labels': ml, 'us': ints(us.hm)},
            'c': {'edges': MONTHLY_CLASSES, 'labels': ml, 'us_owners': ints(us.hco), 'us_renters': ints(us.hm)},
        },
        'sizes': sizes,
        'us': {k: num(us[k]) for k in ('owners', 'renters', 'v', 'p', 'm', 'c', 'co', 'income')},
        'fields': fields,
    }
    with open(os.path.join(OUT, 'manifest.json'), 'w') as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    print('manifest written:', manifest['classes']['v']['labels'], manifest['classes']['m']['labels'])


if __name__ == '__main__':
    main()
