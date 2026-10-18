"""Restate the reference prices at current prices, add monthly costs, and compute area statistics.

Monthly refreshes re-run this step (and the ones after it) with fresh Zillow and FRED downloads; it
needs only work/base/ and work/acs.pkl from a full build, not the census blocks or HMDA.

  Home values   x ZHVI(latest month) / ZHVI(2020-2024 average in CPI-adjusted 2024 dollars) for the
                home's ZIP code (else county, state or the nation). Homes worth less than the ZIP's
                typical mid-tier home follow the mid-tier index, homes worth more than its typical
                top-tier home the top-tier index, in between a log-linear blend.
  Paid rents    x CPI rent of primary residence for the census region, latest month / 2020-2024
                average (rents sitting tenants pay move with this index).
  Market rents  x ZORI(latest month) / ZORI(2023-2024 average) by ZIP, county, state or nation (the
                base market rents describe 2023-24 move-ins).
  Monthly cost  owners: what buying the home today would cost a month: 20% down, 30-year fixed at the
                latest Freddie Mac rate, property tax at the tract's effective rate (median tax /
                median value, ACS; at least 1.1% in California, where a sale resets the tax base),
                insurance at 0.5% of value a year. Renters: market rent.

Inputs  work/base/*, work/acs.pkl, work/econ/*, work/zillow/*, work/hud/*
Output  work/now/<state>.npz   per-dot classes: kv value, kp paid rent, km market rent, kc monthly cost
                               (255 = not this tenure)
        work/now/stats.pkl     per-level statistics (us, state, county, tract, bg, zcta) and ZIP sizes
        work/now/vintage.json  index months, mortgage rate, assumptions
"""
import os, sys, json, pickle, time
import numpy as np, pandas as pd
import econ
from common import (WORK, STATES, ACS_YEAR, HUD_FY, VALUE_CLASSES, MONTHLY_CLASSES, NO_CASH_CLASS, DOWN, TERM_MONTHS,
                    INSURANCE, CA_MIN_TAX)

BASE = os.path.join(WORK, 'base')
NOW = os.path.join(WORK, 'now')
LEVELS = ('us', 'state', 'county', 'tract', 'bg', 'zcta')
VALUE_TOP = 2_000_000.0     # ACS top code for home values


# ---------------------------------------------------------------------------------- factors
class Lookup:
    """A factor table with fallbacks: ZIP -> county -> state -> nation. Each level is a DataFrame
    with the same columns; resolve() picks, per item, the first level with a complete row."""
    def __init__(self, zip_df, county_df, state_df, nation_row):
        self.levels = [zip_df, county_df, state_df]
        self.nation = nation_row

    def resolve(self, zi, ci, si):
        cols = list(self.nation.index)
        out = {c: np.full(len(zi), float(self.nation[c])) for c in cols}
        src = np.full(len(zi), 3, np.int8)
        for lvl in (2, 1, 0):                    # coarse to fine: finer levels overwrite
            df = self.levels[lvl]
            idx = (zi, ci, si)[lvl]
            arr = df[cols].to_numpy()
            okrow = np.isfinite(arr).all(axis=1)
            valid = idx >= 0
            hit = np.zeros(len(zi), bool)
            hit[valid] = okrow[idx[valid]]
            for j, c in enumerate(cols):
                out[c][hit] = arr[idx[hit], j]
            src[hit] = lvl
        return out, src


def zhvi_table(name_mid, name_top, key, index):
    mid = econ.zillow(name_mid, key)
    rm, nm, when = econ.ref_and_now(mid, (ACS_YEAR - 4, ACS_YEAR), 36)
    df = pd.DataFrame({'fmid': nm / rm, 'lmid': rm})
    if name_top:
        top = econ.zillow(name_top, key)
        rt, nt, _ = econ.ref_and_now(top, (ACS_YEAR - 4, ACS_YEAR), 36)
        df['ftop'] = (nt / rt).reindex(df.index)
        df['ltop'] = rt.reindex(df.index)
    else:
        df['ftop'] = df.fmid; df['ltop'] = df.lmid * 2
    # no top-tier series: the mid-tier factor applies throughout
    miss = df.ftop.isna() | df.ltop.isna() | (df.ltop <= df.lmid)
    df.loc[miss, 'ftop'] = df.fmid[miss]; df.loc[miss, 'ltop'] = df.lmid[miss] * 2
    df[['fmid', 'ftop']] = df[['fmid', 'ftop']].clip(0.5, 2.0)
    return df.reindex(index), when, nm, df


def state_rows(raw):
    """State fallback: median over Zillow's own county rows, grouped by their state code (Zillow keeps
    Connecticut's old counties, which match none of the ACS's planning regions)."""
    return raw.groupby(raw.index.str[:2]).median().reindex(STATES)


def value_lookup(meta):
    zips = pd.Index(meta['zcta'])
    counties = pd.Index(meta['counties'])
    z, when, zhvi_now, _ = zhvi_table('Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv',
                                   'Zip_zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month.csv', 'zip', zips)
    c, _, _, craw = zhvi_table('County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv',
                               'County_zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month.csv', 'county', counties)
    s = state_rows(craw)
    m, _, _, _ = zhvi_table('Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv', None, 'metro', ['United States'])
    nat = m.loc['United States']
    print(f'ZHVI {when}: ZIP factors for {z.fmid.notna().sum():,} ZCTAs, {c.fmid.notna().sum():,} counties; '
          f'US mid-tier factor {nat.fmid:.3f}')
    return Lookup(z, c, s, nat), when, zhvi_now


def zori_lookup(meta):
    zips = pd.Index(meta['zcta']); counties = pd.Index(meta['counties'])
    def tab(name, key, index):
        d = econ.zillow(name, key)
        r, n, when = econ.ref_and_now(d, (ACS_YEAR - 1, ACS_YEAR), 12)
        raw = pd.DataFrame({'h': (n / r).clip(0.8, 1.4)})
        return raw.reindex(index), when, n, raw
    z, when, zori_now, _ = tab('Zip_zori_uc_sfrcondomfr_sm_month.csv', 'zip', zips)
    c, _, _, craw = tab('County_zori_uc_sfrcondomfr_sm_month.csv', 'county', counties)
    s = state_rows(craw)
    n, _, _, _ = tab('Metro_zori_uc_sfrcondomfr_sm_month.csv', 'metro', ['United States'])
    print(f'ZORI {when}: ZIP factors for {z.h.notna().sum():,} ZCTAs, {c.h.notna().sum():,} counties; '
          f'US factor {n.h.iloc[0]:.3f}')
    return Lookup(z, c, s, n.loc['United States']), when, zori_now


def tier_factor(v, F):
    lv = np.log(np.maximum(v, 1.0))
    a, b = np.log(F['lmid']), np.log(F['ltop'])
    w = np.clip((lv - a) / np.where(b > a, b - a, 1), 0, 1)
    w = np.where(b > a, w, 0)
    return np.exp((1 - w) * np.log(F['fmid']) + w * np.log(F['ftop']))


def tax_rates(acs):
    """Effective property tax rate (median tax / median value) per tract, county and state. Medians at
    a top code ($10,001 tax, $2,000,001 value) are left out; a tract's own rate is blended with its
    county's by the number of owners (a county rate is worth 300 owners). A tract whose median tax is
    top-coded pays at least $10,001 / median value, so its rate is never set below that."""
    def rate(d):
        ok = (d.med_tax < 10001) & (d.med_value < 2000001) & (d.med_value > 0)
        return (d.med_tax / d.med_value).where(ok)
    t, c, s = acs['tract'], acs['county'], acs['state']
    tr = rate(t)
    # county: owner-weighted median of its tracts' valid rates, else its own ratio, else the state's
    df = pd.DataFrame({'r': tr, 'w': t.owners, 'c': t.index.str[:5]}).dropna()
    df = df.sort_values(['c', 'r'])
    df['cw'] = df.groupby('c').w.cumsum(); df['tot'] = df.groupby('c').w.transform('sum')
    cmed = df[df.cw >= df.tot / 2].groupby('c').r.first()
    ntr = df.groupby('c').size()
    sr = rate(s).reindex(STATES)
    cr = cmed.where(ntr.reindex(cmed.index) >= 3).reindex(c.index)
    cr = cr.fillna(rate(c)).fillna(pd.Series(sr.reindex(c.index.str[:2]).to_numpy(), index=c.index))
    n = t.owners.fillna(0)
    prior = cr.reindex(t.index.str[:5]).to_numpy()
    own = tr.to_numpy()
    blend = np.where(np.isfinite(own), (n * np.nan_to_num(own) + 300 * prior) / (n + 300), prior)
    blend = np.fmax(blend, min_rate(t).to_numpy())
    tt = pd.Series(np.clip(blend, 0.001, 0.04), index=t.index)
    ca = t.index.str[:2] == '06'
    tt[ca] = np.maximum(tt[ca], CA_MIN_TAX)
    cr = cr.clip(0.001, 0.04)
    cr[c.index.str[:2] == '06'] = np.maximum(cr[c.index.str[:2] == '06'], CA_MIN_TAX)
    sr['06'] = max(sr['06'], CA_MIN_TAX)
    return tt, cr, sr


def min_rate(d):
    """Lower bound on the rate where the median tax is top-coded ($10,001+) but the median value isn't."""
    top = (d.med_tax >= 10001) & (d.med_value < 2000001) & (d.med_value > 0)
    return (10001 / d.med_value).where(top)


def monthly_owner(price, tax, rate):
    i = rate / 12
    pi = (1 - DOWN) * price * i / (1 - (1 + i) ** -TERM_MONTHS)
    return pi + price * (tax + INSURANCE) / 12


def classify(v, edges):
    k = np.searchsorted(np.asarray(edges, float), v, side='right') - 1
    return np.where(np.isfinite(v), np.clip(k, 0, len(edges) - 1), 255).astype(np.uint8)


# ---------------------------------------------------------------------------------- statistics
def group_median(key, val, w, nkeys):
    """Weighted median per key, interpolating between weighted midpoints (each quadrature point
    stands for an even slice of its bracket, so this matches the Census interpolation)."""
    ok = np.isfinite(val) & (w > 0) & (key >= 0)
    key, val, w = key[ok], val[ok], w[ok]
    out = np.full(nkeys, np.nan)
    if not len(key): return out
    o = np.lexsort((val, key))
    key, val, w = key[o], val[o], w[o]
    start = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    cnt = np.diff(np.r_[start, len(key)])
    grp = np.repeat(np.arange(len(start)), cnt)
    cw = np.cumsum(w)
    base = np.r_[0, cw][start]
    tot = np.add.reduceat(w, start)
    pos = grp + (cw - base[grp] - w / 2) / tot[grp]
    target = np.arange(len(start)) + 0.5
    j = np.searchsorted(pos, target)
    first, last = start, start + cnt - 1
    jl = np.clip(j - 1, first, last); jr = np.clip(j, first, last)
    span = pos[jr] - pos[jl]
    f = np.where(span > 0, np.clip((target - pos[jl]) / np.where(span > 0, span, 1), 0, 1), 0)
    out[key[start]] = val[jl] + f * (val[jr] - val[jl])
    return out


def hist(key, cls, w, nkeys, ncls):
    ok = (key >= 0) & (cls < ncls)
    return np.bincount(key[ok] * ncls + cls[ok], weights=w[ok], minlength=nkeys * ncls).reshape(nkeys, ncls)


def main():
    t0 = time.time()
    os.makedirs(NOW, exist_ok=True)
    meta = pickle.load(open(os.path.join(BASE, 'meta.pkl'), 'rb'))
    acs = pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))
    bg_ids = pd.Index(meta['bg']); counties = pd.Index(meta['counties']); tracts = pd.Index(meta['tracts'])
    zips = pd.Index(meta['zcta'])
    bg_county = pd.Series(np.arange(len(counties)), index=counties).reindex(bg_ids.str[:5]).to_numpy()
    bg_tract = pd.Series(np.arange(len(tracts)), index=tracts).reindex(bg_ids.str[:11]).to_numpy()
    spos = {s: i for i, s in enumerate(STATES)}
    bg_state = np.array([spos[s] for s in bg_ids.str[:2]])
    bg_zcta = meta['bg_zcta']

    V, zhvi_month, zhvi_now = value_lookup(meta)
    H, zori_month, zori_now = zori_lookup(meta)
    region = np.array([econ.REGION[s] for s in STATES])
    gfac = {r: econ.rent_cpi_factor(r) for r in (1, 2, 3, 4)}
    cpi_month = gfac[1][1]
    g_state = np.array([gfac[r][0] for r in region])
    rate, rate_date = econ.mortgage_rate()
    tau_t, tau_c, tau_s = tax_rates(acs)
    tau_bg = tau_t.reindex(tracts).to_numpy()[bg_tract]
    c = econ.cpi()
    cpi_now = float(c.iloc[-1]); cpi_ref = float(c[c.index.year == ACS_YEAR].mean())
    print(f'paid-rent CPI factors by region {[round(gfac[r][0], 3) for r in (1, 2, 3, 4)]} ({cpi_month}); '
          f'mortgage {rate * 100:.2f}% ({rate_date}); CPI {cpi_now / cpi_ref:.3f}x 2024 dollars')

    # how each block group's homes split across ZIP codes (by its dots), for ZIP statistics, and each
    # ZIP's main county (most dots), for ZIP-level fallbacks
    gz = []
    for st in STATES:
        d = np.load(os.path.join(BASE, st + '.npz'))
        ok = d['z'] >= 0
        gz.append(pd.DataFrame({'g': d['g'][ok].astype(np.int64), 'z': d['z'][ok].astype(np.int64)}))
    pairs = pd.concat(gz).value_counts().rename('n').reset_index().sort_values(['g', 'z'])
    pairs['share'] = pairs.n / pairs.groupby('g').n.transform('sum')
    zc = pairs.assign(c=bg_county[pairs.g.to_numpy()]).groupby(['z', 'c']).n.sum().reset_index()
    zc = zc.sort_values(['z', 'n'], ascending=[True, False]).drop_duplicates('z')
    zip_county = np.full(len(zips), -1); zip_county[zc.z.to_numpy()] = zc.c.to_numpy()
    zip_state = np.array([spos.get(c[:2], -1) if c else -1 for c in np.where(zip_county >= 0, counties.to_numpy()[np.maximum(zip_county, 0)], '')])
    pg, pz, ps = pairs.g.to_numpy(), pairs.z.to_numpy(), pairs.share.to_numpy()
    pstart = np.searchsorted(pg, np.arange(len(bg_ids))); pend = np.searchsorted(pg, np.arange(len(bg_ids)), 'right')

    def by_zip(g):
        """Expand items in block groups g over their ZIP codes: (item index, ZIP, share of the item)."""
        cnt = (pend - pstart)[g]
        idx = np.repeat(np.arange(len(g)), cnt)
        j = pstart[g][idx] + (np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt))
        return idx, pz[j], ps[j]

    M_tract = meta['mratio'].reindex(tracts).fillna(1.0).to_numpy()
    k_rent_zip = np.full(len(zips), np.nan)     # market-rent calibration to ZORI, per ZIP
    k_rent_county = np.ones(len(counties))      # ... and its county-wide median, for ZIPs without ZORI
    k_top_zip = np.ones(len(zips))              # stretch of values above the top code, per ZIP

    def price(z, g, t, nc, ref, q):
        """Current value / paid rent / market rent / monthly cost for items in block groups g, ZCTAs z."""
        ci, si = bg_county[g], bg_state[g]
        F, vsrc = V.resolve(z, ci, si)
        Hf, hsrc = H.resolve(z, ci, si)
        own = t == 0
        f = tier_factor(ref, F)
        value = ref * f
        top = own & (ref >= VALUE_TOP)
        T = VALUE_TOP * f
        kt = np.where(z >= 0, k_top_zip[np.maximum(z, 0)], 1.0)
        value = np.where(top, T + (value - T) * kt, value)
        value = np.where(own, value, np.nan)
        paid = np.where(~own & ~nc, ref * g_state[si], np.nan)
        kr = np.where(z >= 0, k_rent_zip[np.maximum(z, 0)], np.nan)
        kr = np.where(np.isfinite(kr), kr, k_rent_county[ci])
        Mk = np.minimum(M_tract[bg_tract[g]] * kr, 3.0)
        market = np.where(~own, ref * Mk ** (2 * (1 - np.nan_to_num(q, nan=0.5))) * Hf['h'], np.nan)
        cost = np.where(own, monthly_owner(value, tau_bg[g], rate), market)
        return value, paid, market, cost, vsrc, hsrc

    # ------------------------------------------------------------------ calibration (area points)
    P = np.load(os.path.join(BASE, 'points.npz'))
    g = P['g'].astype(np.int64); t = P['t']; nc = P['nc']; w = P['w'].astype(float)
    pref, pq = P['ref'].astype(float), P['q'].astype(float)
    z = bg_zcta[g].astype(np.int64)            # prices use the block group's main ZIP's indexes
    own = t == 0
    ei, zkey, es = by_zip(g)                    # ZIP statistics split each block group by its dots
    we = w[ei] * es
    n_own = np.bincount(zkey[own[ei]], weights=we[own[ei]], minlength=len(zips))
    n_ren = np.bincount(zkey[~own[ei]], weights=we[~own[ei]], minlength=len(zips))
    zori_z = zori_now.reindex(zips).to_numpy()
    zhvi_z = zhvi_now.reindex(zips).to_numpy()
    # market rents: where Zillow observes asking rents (ZORI), raise the ZIP's market rents to at
    # least that level through the regulated end (the uplift M), so homes already let at market
    # rates stay put. ZORI excludes utilities, so it is a floor, not a target, for gross rents.
    cal = np.isfinite(zori_z) & (n_ren >= 200)
    for _ in range(3):
        _, _, market, _, _, _ = price(z, g, t, nc, pref, pq)
        med = group_median(zkey, market[ei], we, len(zips))
        step = np.where(cal & np.isfinite(med), zori_z / med, 1.0)
        k_rent_zip = np.where(cal, np.clip(np.nan_to_num(k_rent_zip, nan=1.0) * step, 1.0, 1.6), np.nan)
    kz = pd.DataFrame({'k': k_rent_zip, 'c': zip_county, 'w': n_ren})[cal & (zip_county >= 0)]
    kz = kz.sort_values(['c', 'k']); kz['cw'] = kz.groupby('c').w.cumsum(); kz['tot'] = kz.groupby('c').w.transform('sum')
    kcm = kz[kz.cw >= kz.tot / 2].groupby('c').k.first()
    k_rent_county[kcm.index.to_numpy()] = kcm.to_numpy()
    print(f'market rents calibrated to ZORI in {cal.sum():,} ZIPs: uplift x{np.percentile(k_rent_zip[cal], [50, 90, 99]).round(2)} '
          f'(50/90/99th pct); {(k_rent_county > 1).sum():,} counties carry it to ZIPs without ZORI')
    # home values: in ZIPs where most homes are above the $2M top code, scale the excess over the
    # top code so the ZIP's median sits where the ZHVI says, relative to ZIPs the ACS fully describes
    value, _, _, _, _, _ = price(z, g, t, nc, pref, pq)
    med = group_median(zkey, value[ei], we, len(zips))
    tb = (own & (pref >= VALUE_TOP))[ei]
    topw = np.bincount(zkey[tb], weights=we[tb], minlength=len(zips))
    share = topw / np.maximum(n_own, 1)
    clean = (share < 0.2) & (n_own >= 300) & np.isfinite(zhvi_z) & np.isfinite(med)
    rho = float(np.median(med[clean] / zhvi_z[clean]))
    Fz, _ = V.resolve(np.arange(len(zips)), zip_county, zip_state)
    Tz = VALUE_TOP * Fz['ftop']
    # (only where the ZHVI target is itself above the top code)
    tcal = (share >= 0.5) & (n_own >= 200) & np.isfinite(zhvi_z) & np.isfinite(med) & (rho * zhvi_z > Tz * 1.05)
    for _ in range(2):
        value, _, _, _, _, _ = price(z, g, t, nc, pref, pq)
        med = group_median(zkey, value[ei], we, len(zips))
        ok = tcal & (med > Tz * 1.02)
        k_top_zip = np.where(ok, np.clip(k_top_zip * (rho * zhvi_z - Tz) / np.where(ok, med - Tz, 1), 0.7, 3.0), k_top_zip)
    print(f'values: model/ZHVI median ratio {rho:.3f} where the ACS covers the distribution; '
          f'{tcal.sum()} ZIPs mostly above the top code stretched x{np.percentile(k_top_zip[tcal], [10, 50, 90]).round(2)}')

    # ------------------------------------------------------------------ dots
    srcs = np.zeros((2, 4), np.int64)
    for st in STATES:
        d = np.load(os.path.join(BASE, st + '.npz'))
        dg = d['g'].astype(np.int64)
        value, paid, market, cost, vsrc, hsrc = price(d['z'].astype(np.int64), dg, d['t'], d['nc'], d['ref'].astype(float), d['q'].astype(float))
        downer = d['t'] == 0
        kp = classify(paid, MONTHLY_CLASSES)
        kp[~downer & d['nc']] = NO_CASH_CLASS
        np.savez(os.path.join(NOW, st + '.npz'), kv=classify(value, VALUE_CLASSES), kp=kp,
                 km=classify(market, MONTHLY_CLASSES), kc=classify(cost, MONTHLY_CLASSES))
        srcs[0] += np.bincount(vsrc[downer], minlength=4); srcs[1] += np.bincount(hsrc[~downer], minlength=4)
    print('value index source (ZIP, county, state, US) for owner dots:', (srcs[0] / srcs[0].sum()).round(3),
          '| rent index for renter dots:', (srcs[1] / srcs[1].sum()).round(3))

    # ------------------------------------------------------------------ area statistics
    value, paid, market, cost, _, _ = price(z, g, t, nc, pref, pq)
    kv = classify(value, VALUE_CLASSES).astype(np.int64)
    kp = classify(paid, MONTHLY_CLASSES).astype(np.int64); kp[(t == 1) & nc] = NO_CASH_CLASS
    km = classify(market, MONTHLY_CLASSES).astype(np.int64)
    kco = np.where(t == 0, classify(cost, MONTHLY_CLASSES), 255).astype(np.int64)
    keys = {'us': (np.zeros(len(g), np.int64), 1, ['us']),
            'state': (bg_state[g], len(STATES), STATES),
            'county': (bg_county[g], len(counties), counties),
            'tract': (bg_tract[g], len(tracts), tracts),
            'bg': (g, len(bg_ids), bg_ids),
            'zcta': (zkey, len(zips), zips)}
    nv, nm = len(VALUE_CLASSES), len(MONTHLY_CLASSES)
    stats = {}
    base_arrays = (w, own, value, paid, market, cost, kv, kp, km, kco)
    for lvl, (key, nk, index) in keys.items():
        if lvl == 'zcta':
            w, own, value, paid, market, cost, kv, kp, km, kco = (we,) + tuple(a[ei] for a in base_arrays[1:])
        else:
            w, own, value, paid, market, cost, kv, kp, km, kco = base_arrays
        df = pd.DataFrame(index=pd.Index(index, name='geoid'))
        df['owners'] = np.bincount(key[own & (key >= 0)], weights=w[own & (key >= 0)], minlength=nk)
        df['renters'] = np.bincount(key[~own & (key >= 0)], weights=w[~own & (key >= 0)], minlength=nk)
        df['v'] = group_median(key, value, w, nk)
        df['p'] = group_median(key, paid, w, nk)
        df['m'] = group_median(key, market, w, nk)
        df['c'] = group_median(key, cost, w, nk)
        df['co'] = group_median(key, np.where(own, cost, np.nan), w, nk)
        df['hv'] = list(hist(key, kv, w, nk, nv))
        df['hp'] = list(hist(key, kp, w, nk, nm + 1))
        df['hm'] = list(hist(key, km, w, nk, nm))
        df['hco'] = list(hist(key, kco, w, nk, nm))
        stats[lvl] = df
        print(f'{lvl}: {nk:,} areas ({time.time() - t0:.0f}s)', flush=True)
    w, own, value, paid, market, cost, kv, kp, km, kco = base_arrays
    us = stats['us'].iloc[0]
    print(f'US medians: value ${us.v:,.0f}, paid rent ${us.p:,.0f}, market rent ${us.m:,.0f}, '
          f'monthly cost ${us.c:,.0f} (owners buying today ${us.co:,.0f})')

    # ------------------------------------------------------------------ extra ACS fields
    infl = cpi_now / cpi_ref
    for lvl in LEVELS:
        a = acs[lvl] if lvl != 'state' else acs['state'].reindex(STATES)
        a = a.reindex(stats[lvl].index) if lvl != 'us' else a.set_axis(['us'])
        s = stats[lvl]
        for col in ('units', 'vacant', 'rent_burden'):
            s[col] = a[col].to_numpy()
        s['income'] = a.med_income.to_numpy() * infl
        s['synth'] = a['synth'].fillna(0).to_numpy() if 'synth' in a else 0
        s['tax'] = {'tract': tau_t.reindex(s.index).to_numpy(), 'county': tau_c.reindex(s.index).to_numpy(),
                    'state': tau_s.reindex(s.index).to_numpy(), 'bg': tau_bg}.get(lvl, np.nan)
    stats['tract']['mratio'] = meta['mratio'].reindex(tracts).to_numpy()
    stats['zcta']['k_rent'] = k_rent_zip
    stats['zcta']['k_top'] = k_top_zip

    # ------------------------------------------------------------------ ZIP code sizes
    sizes = pd.DataFrame(index=zips)
    for b in range(1, 6):
        d = econ.zillow(f'Zip_zhvi_bdrmcnt_{b}_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv', 'zip')
        sizes[f'v{b}'] = d[d.columns.max()].reindex(zips)
    hud = pd.read_excel(os.path.join(WORK, 'hud', f'fy{HUD_FY}_safmrs.xlsx'), dtype={0: str})
    hud.columns = ['zip', 'area', 'name'] + [f'c{i}' for i in range(len(hud.columns) - 3)]
    hud['zip'] = hud.zip.str.zfill(5)
    hz = hud.groupby('zip')[[f'c{3 * b}' for b in range(5)]].mean()
    for b in range(5):
        sizes[f'm{b}'] = hz[f'c{3 * b}'].reindex(zips)
    # ZIPs outside the Small Area FMR metros: the county's Fair Market Rent
    fmr = pd.read_excel(os.path.join(WORK, 'hud', f'fy{HUD_FY}_fmrs.xlsx'), dtype={'fips': str})
    fmr['county'] = fmr.fips.str.zfill(10).str[:5]
    fc = fmr.groupby('county')[[f'fmr_{b}' for b in range(5)]].mean()
    zcode = pd.Index(np.where(zip_county >= 0, counties.to_numpy()[np.maximum(zip_county, 0)], ''))
    for b in range(5):
        miss = sizes[f'm{b}'].isna().to_numpy()
        sizes.loc[miss, f'm{b}'] = fc[f'fmr_{b}'].reindex(zcode[miss]).to_numpy()
    za = acs['zcta'].reindex(zips)
    zstate = pd.Index(zcode.str[:2])
    gz = np.array([gfac[econ.REGION.get(s, 3)][0] for s in zstate])
    for b in range(5):
        col = za[f'rb{b}'].where(za[f'rb{b}'] < 3501)
        sizes[f'p{b}'] = col.to_numpy() * gz
    zt = ((za.med_tax / za.med_value).where((za.med_tax < 10001) & (za.med_value < 2000001) & (za.med_value > 0))).to_numpy()
    zt = np.where(np.isfinite(zt), zt, tau_c.reindex(zcode).to_numpy())
    zt = np.where(np.isfinite(zt), zt, np.nanmedian(tau_c))
    zt = np.fmax(zt, min_rate(za).to_numpy())
    zt = np.clip(zt, 0.001, 0.04)
    zt = np.where(zstate == '06', np.maximum(zt, CA_MIN_TAX), zt)
    for b in range(1, 6):
        sizes[f'c{b}'] = monthly_owner(sizes[f'v{b}'].to_numpy(), zt, rate)
    sizes['zhvi'] = zhvi_now.reindex(zips).to_numpy()
    sizes['zori'] = zori_now.reindex(zips).to_numpy()
    sizes['county'] = zcode
    sizes['tax'] = zt
    stats['zcta']['tax'] = zt
    stats['sizes'] = sizes
    print(f'ZIP sizes: home values for {sizes.v2.notna().sum():,} ZIPs (2 bd), market rents {sizes.m2.notna().sum():,}, '
          f'paid rents {sizes.p2.notna().sum():,}')

    pickle.dump(stats, open(os.path.join(NOW, 'stats.pkl'), 'wb'))
    vintage = {'zhvi': zhvi_month, 'zori': zori_month, 'cpi': cpi_month, 'mortgage_rate': rate, 'mortgage_date': rate_date,
               'acs': f'{ACS_YEAR - 4}-{ACS_YEAR}', 'hud_fy': HUD_FY, 'hmda': '2024-2025', 'down': DOWN,
               'insurance': INSURANCE, 'ca_min_tax': CA_MIN_TAX, 'income_factor': infl}
    json.dump(vintage, open(os.path.join(NOW, 'vintage.json'), 'w'), indent=1)
    print(f'done ({time.time() - t0:.0f}s)')


if __name__ == '__main__':
    main()
