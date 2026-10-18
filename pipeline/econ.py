"""Price indexes used to put ACS dollars into current dollars.

ACS 5-year dollar figures are survey answers from 2020-2024, each inflated to 2024 dollars with the
CPI. To restate a figure at today's prices we divide by the index's 2020-2024 average (expressed the
same way, in CPI-adjusted 2024 dollars) and multiply by the index's latest value.

Inputs (work/):
  econ/CPIAUCNS.csv                   CPI-U, all items (FRED)
  econ/CUUR0[1-4]00SEHA.csv           CPI-U rent of primary residence, by census region (FRED)
  econ/mortgage30.csv                 Freddie Mac 30-year fixed rate, weekly (FRED MORTGAGE30US)
  zillow/*.csv                        Zillow Home Value Index (ZHVI) and Observed Rent Index (ZORI)
"""
import os
import numpy as np, pandas as pd
from common import WORK, ACS_YEAR

ECON = os.path.join(WORK, 'econ')
ZILLOW = os.path.join(WORK, 'zillow')

# census region of each state (for the regional rent CPI): 1 Northeast, 2 Midwest, 3 South, 4 West
REGION = {s: r for r, ss in {
    1: '09 23 25 33 34 36 42 44 50', 2: '17 18 19 20 26 27 29 31 38 39 46 55',
    3: '01 05 10 11 12 13 21 22 24 28 37 40 45 47 48 51 54 72',
    4: '02 04 06 08 15 16 30 32 35 41 49 53 56'}.items() for s in ss.split()}


def fred(name):
    df = pd.read_csv(os.path.join(ECON, name + '.csv'))
    s = pd.to_numeric(df.iloc[:, 1], errors='coerce')
    s.index = pd.PeriodIndex(pd.to_datetime(df.iloc[:, 0]), freq='M')
    return s.dropna()


def cpi():
    return fred('CPIAUCNS')


def to_ref(series_m, cpi_m, years):
    """Average of a monthly series over `years` (inclusive range), each month restated in
    ACS_YEAR dollars with the CPI: the same basis as ACS 5-year dollar figures."""
    base = cpi_m[(cpi_m.index.year == ACS_YEAR)].mean()
    sel = (series_m.index.year >= years[0]) & (series_m.index.year <= years[1])
    s = series_m[sel]
    return (s * base / cpi_m.reindex(s.index)).mean()


def rent_cpi_factor(region, when=None):
    """Sitting tenants' rents: CPI rent of primary residence for the census region, latest month
    over its 2020-2024 average in 2024 dollars."""
    c = cpi()
    r = fred(f'CUUR0{region}00SEHA')
    now = r.index.max() if when is None else when
    return float(r[now] / to_ref(r, c, (ACS_YEAR - 4, ACS_YEAR))), str(now)


def mortgage_rate():
    df = pd.read_csv(os.path.join(ECON, 'mortgage30.csv')).dropna()
    df = df[pd.to_numeric(df.iloc[:, 1], errors='coerce').notna()]
    return float(df.iloc[-1, 1]) / 100, str(df.iloc[-1, 0])


def zillow(name, key):
    """Monthly Zillow series as a DataFrame (rows = regions, columns = monthly Periods).
    key: 'zip' (5-digit ZIP), 'county' (5-digit FIPS) or 'metro' (RegionName)."""
    df = pd.read_csv(os.path.join(ZILLOW, name), dtype={'RegionName': str, 'StateCodeFIPS': str, 'MunicipalCodeFIPS': str})
    if key == 'zip':
        idx = df.RegionName.str.zfill(5)
    elif key == 'county':
        idx = df.StateCodeFIPS.str.zfill(2) + df.MunicipalCodeFIPS.str.zfill(3)
    else:
        idx = df.RegionName
    cols = [c for c in df.columns if c[:2] in ('19', '20') and c[4] == '-']
    out = df[cols].apply(pd.to_numeric, errors='coerce')
    out.columns = pd.PeriodIndex(pd.to_datetime(cols), freq='M')
    out.index = idx
    return out[~out.index.duplicated()]


def ref_and_now(df, years, min_months, when=None):
    """Per region: (reference level = average over `years` in ACS_YEAR dollars, latest level).
    Regions with fewer than min_months observations in the window, or no latest value, are NaN."""
    c = cpi()
    base = c[c.index.year == ACS_YEAR].mean()
    cols = [p for p in df.columns if years[0] <= p.year <= years[1]]
    win = df[cols]
    real = win * (base / c.reindex(cols).to_numpy())
    ref = real.mean(axis=1).where(win.notna().sum(axis=1) >= min_months)
    now_col = df.columns.max() if when is None else when
    return ref, df[now_col], str(now_col)


def year_mean(df, year):
    cols = [p for p in df.columns if p.year == year]
    return df[cols].mean(axis=1)
