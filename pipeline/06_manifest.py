"""Write data/manifest.json, the index the web app loads first."""
import os, json, pickle, datetime
from common import WORK, OUT, VALUE_EDGES, RENT_EDGES, VALUE_CLASSES, RENT_CLASSES, FINEST, ACS_YEAR

def num(x):
    return None if x != x else int(round(x))

def main():
    tiers = json.load(open(os.path.join(WORK, 'tiers.json')))
    files = json.load(open(os.path.join(WORK, 'areas.json')))
    FIELDS = json.load(open(os.path.join(WORK, 'fields.json')))['fields']
    us = pickle.load(open(os.path.join(WORK, 'us.pkl'), 'rb')).iloc[0]
    manifest = {
        'built': datetime.date.today().isoformat(),
        'acs': f'{ACS_YEAR - 4}–{ACS_YEAR} ACS 5-year estimates',
        'finest': FINEST,
        'tiers': tiers,
        'areas': files,
        # us_classes: homes per display class nationwide (the legend's share bars)
        'value': {'edges': VALUE_EDGES, 'classes': [[l, lo, hi] for l, lo, hi in VALUE_CLASSES],
                  'us_median': num(us.med_value), 'us_total': num(us.val_total), 'us_classes': [int(c) for c in us.vc]},
        'rent': {'edges': RENT_EDGES, 'classes': [[l, lo, hi] for l, lo, hi in RENT_CLASSES],
                 'us_median': num(us.med_rent), 'us_total': num(us.rent_total), 'us_classes': [int(c) for c in us.rc]},
        'fields': FIELDS,
    }
    with open(os.path.join(OUT, 'manifest.json'), 'w') as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    print('manifest written')


if __name__ == '__main__':
    main()
