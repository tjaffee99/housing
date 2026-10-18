"""Download the inputs that change month to month (Zillow, FRED), and with --all everything else a
full build needs except the census blocks (see README) and HMDA (tools/fetch_hmda.py).

Run from pipeline/:   python3 tools/fetch_sources.py            # monthly: Zillow + FRED
                      python3 tools/fetch_sources.py --geo      # + Census outlines (the refresh needs these)
                      python3 tools/fetch_sources.py --all      # + ACS tables and HUD rents
"""
import os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import WORK, ACS_YEAR, HUD_FY

UA = 'housing.theojaffee.net-data-refresh/1.0'
# HUD's site only serves browsers (and FRED turns away browser user agents that don't act like one)
BROWSER_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'
Z = 'https://files.zillowstatic.com/research/public_csvs'
MID, TOP = 'uc_sfrcondo_tier_0.33_0.67_sm_sa_month', 'uc_sfrcondo_tier_0.67_1.0_sm_sa_month'
ZILLOW = ([f'zhvi/{g}_zhvi_{t}.csv' for g in ('Zip', 'County') for t in (MID, TOP)] + [f'zhvi/Metro_zhvi_{MID}.csv'] +
          [f'zhvi/Zip_zhvi_bdrmcnt_{b}_{MID}.csv' for b in range(1, 6)] +
          [f'zori/{g}_zori_uc_sfrcondomfr_sm_month.csv' for g in ('Zip', 'County', 'Metro')])
FRED = {'CPIAUCNS': 'CPIAUCNS', 'MORTGAGE30US': 'mortgage30', **{f'CUUR0{r}00SEHA': f'CUUR0{r}00SEHA' for r in range(5)}}
GEO = {'cb_state': f'GENZ{ACS_YEAR}/shp/cb_{ACS_YEAR}_us_state_500k.zip', 'cb_county': f'GENZ{ACS_YEAR}/shp/cb_{ACS_YEAR}_us_county_500k.zip',
       'cb_tract': f'GENZ{ACS_YEAR}/shp/cb_{ACS_YEAR}_us_tract_500k.zip', 'cb_bg': f'GENZ{ACS_YEAR}/shp/cb_{ACS_YEAR}_us_bg_500k.zip',
       'cb_nation': f'GENZ{ACS_YEAR}/shp/cb_{ACS_YEAR}_us_nation_5m.zip', 'cb_place': f'GENZ{ACS_YEAR}/shp/cb_{ACS_YEAR}_us_place_500k.zip',
       'cb_zcta': 'GENZ2020/shp/cb_2020_us_zcta520_500k.zip'}
ACS = ['b25075', 'b25063', 'b25077', 'b25064', 'b25003', 'b25071', 'b19013', 'b25001', 'b25002', 'b25113', 'b25031', 'b25103', 'b25042']
HUD = {f'fy{HUD_FY}_safmrs.xlsx': f'fmr{HUD_FY}/FY{HUD_FY % 100}_safmrs.xlsx', f'fy{HUD_FY}_fmrs.xlsx': f'fmr{HUD_FY}/FY{HUD_FY % 100}_FMRs.xlsx'}


def get(url, dst, tries=4):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': BROWSER_UA if 'huduser.gov' in url else UA, 'Accept': '*/*'})
            with urllib.request.urlopen(req, timeout=300) as r, open(dst + '.part', 'wb') as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk: break
                    f.write(chunk)
            os.replace(dst + '.part', dst)
            print(f'{os.path.getsize(dst) / 1e6:8.1f} MB  {os.path.relpath(dst, WORK)}', flush=True)
            return
        except Exception as e:
            print(f'retry {k + 1}: {url}: {e}', flush=True)
            time.sleep(5 * 2 ** k)
    raise SystemExit(f'failed: {url}')


def main():
    for p in ZILLOW:
        get(f'{Z}/{p}', os.path.join(WORK, 'zillow', os.path.basename(p)))
    for sid, name in FRED.items():
        get(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}', os.path.join(WORK, 'econ', name + '.csv'))
    if '--geo' in sys.argv or '--all' in sys.argv:
        for name, path in GEO.items():
            get(f'https://www2.census.gov/geo/tiger/{path}', os.path.join(WORK, 'geo', name + '.zip'))
    if '--all' in sys.argv:
        for t in ACS:
            get(f'https://www2.census.gov/programs-surveys/acs/summary_file/{ACS_YEAR}/table-based-SF/data/5YRData/acsdt5y{ACS_YEAR}-{t}.dat',
                os.path.join(WORK, 'acs', t + '.dat'))
        for name, path in HUD.items():
            get(f'https://www.huduser.gov/portal/datasets/fmr/{path}', os.path.join(WORK, 'hud', name))


if __name__ == '__main__':
    main()
