"""Download HMDA purchase-loan originations (one CSV per state and year) and keep the columns the
map uses. Run from pipeline/: python3 tools/fetch_hmda.py 2024 2025"""
import csv, io, os, sys, gzip, urllib.request, concurrent.futures as cf
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import WORK, STATES

ABBR = {'01': 'AL', '02': 'AK', '04': 'AZ', '05': 'AR', '06': 'CA', '08': 'CO', '09': 'CT', '10': 'DE', '11': 'DC', '12': 'FL',
        '13': 'GA', '15': 'HI', '16': 'ID', '17': 'IL', '18': 'IN', '19': 'IA', '20': 'KS', '21': 'KY', '22': 'LA', '23': 'ME',
        '24': 'MD', '25': 'MA', '26': 'MI', '27': 'MN', '28': 'MS', '29': 'MO', '30': 'MT', '31': 'NE', '32': 'NV', '33': 'NH',
        '34': 'NJ', '35': 'NM', '36': 'NY', '37': 'NC', '38': 'ND', '39': 'OH', '40': 'OK', '41': 'OR', '42': 'PA', '44': 'RI',
        '45': 'SC', '46': 'SD', '47': 'TN', '48': 'TX', '49': 'UT', '50': 'VT', '51': 'VA', '53': 'WA', '54': 'WV', '55': 'WI',
        '56': 'WY', '72': 'PR'}
KEEP = ['census_tract', 'property_value', 'loan_amount', 'occupancy_type', 'total_units', 'derived_dwelling_category']
OUT = os.path.join(WORK, 'hmda')

def fetch(year, st):
    path = os.path.join(OUT, f'{year}_{st}.csv.gz')
    if os.path.exists(path): return path, 'cached'
    url = f'https://ffiec.cfpb.gov/v2/data-browser-api/view/csv?states={ABBR[st]}&years={year}&actions_taken=1&loan_purposes=1'
    n = 0
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=600) as r, gzip.open(path + '.part', 'wt', newline='') as f:
                rd = csv.DictReader(io.TextIOWrapper(r, encoding='utf-8'))
                w = csv.writer(f); w.writerow(KEEP)
                for row in rd:
                    w.writerow([row.get(k, '') for k in KEEP]); n += 1
            os.replace(path + '.part', path)
            return path, n
        except Exception as e:
            err = e
    return path, f'FAILED {err}'

if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    jobs = [(y, st) for y in sys.argv[1:] for st in STATES]
    with cf.ThreadPoolExecutor(4) as ex:
        for (y, st), (p, n) in zip(jobs, ex.map(lambda a: fetch(*a), jobs)):
            print(y, st, n, flush=True)
