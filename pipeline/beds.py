"""Bedrooms for every dot (studio, 1, 2, 3, 4, 5+), used by 03_base.py.

The ACS counts each block group's homes by tenure and bedrooms (B25042) but publishes no prices by
bedrooms there. Each dot gets a bedroom count so that its block group's counts come out right
(systematic rounding: exact totals, unbiased per size), and within a block group the bigger homes
tend to be the pricier ones: a block group's dots of one tenure are ordered by

    RHO * z + sqrt(1 - RHO^2) * noise,      z the normal score of the dot's price rank there,

and take the smallest sizes first, except that studios share the 1-bedroom place and are picked
at random from those dots (the cheapest rents in a neighborhood are more often long-held larger
homes than studios; this matches published studio rents far better). RHO is fitted per tenure, on
every third tract, so that medians by bedrooms come out right on average: for renters, tract medians of gross rent by bedrooms (B25031, the same rents the
dots carry); for owners, ZIP-code ratios between Zillow's home value indexes by bedroom count
(averaged over 2020-2024, the survey years) against the same ratios among the dots.
"""
import os
import numpy as np, pandas as pd
from common import WORK

SIZES = 6
GRID = np.round(np.arange(0.2, 0.801, 0.05), 2)
FIT_SHARE = 3           # the fit uses the dots of every third tract
MIN_DOTS = 6            # dots a (tract or ZIP, bedrooms) cell needs to enter the fit


def ndtri(p):
    """Inverse standard normal CDF (Acklam's rational approximation, |error| < 1.2e-9)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    p = np.asarray(p, float)
    out = np.empty_like(p)
    lo, hi = p < 0.02425, p > 1 - 0.02425
    mid = ~lo & ~hi
    q = p[mid] - 0.5; r = q * q
    out[mid] = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    for m, sign in ((lo, 1), (hi, -1)):
        q = np.sqrt(-2 * np.log(p[m] if sign == 1 else 1 - p[m]))
        out[m] = sign * (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                 ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    return out


def shares(acs, tenure):
    """Each block group's share of homes of one tenure by bedrooms, cumulative (n_bg x 6). Block
    groups without such homes borrow their tract's mix, else their county's, state's or the nation's."""
    bg = acs['bg']
    C = bg[[f'beds_{"or"[tenure]}{k}' for k in range(SIZES)]].fillna(0).to_numpy()
    out = np.full(C.shape, np.nan)
    for key in (bg.index.to_numpy(), bg.index.str[:11], bg.index.str[:5], bg.index.str[:2], np.zeros(len(bg), int)):
        agg = pd.DataFrame(C).groupby(np.asarray(key)).transform('sum').to_numpy()
        tot = agg.sum(1, keepdims=True)
        fill = np.isnan(out[:, 0]) & (tot[:, 0] > 0)
        out[fill] = agg[fill] / tot[fill]
    return np.cumsum(out, 1)


class Ordering:
    """Dots of one tenure, grouped by block group, with the fixed parts of the draw (price ranks,
    noise, rounding offsets), so that bedrooms can be assigned for any RHO. With `studio_with_one`,
    studios share the 1-bedroom place in the order and are picked at random among those dots."""
    def __init__(self, g, price, cum, rng, studio_with_one=False):
        n = len(g)
        o = np.lexsort((rng.random(n), price, g))
        gs = g[o]
        start = np.r_[0, np.flatnonzero(gs[1:] != gs[:-1]) + 1]
        cnt = np.diff(np.r_[start, n])
        self.grp = np.empty(n, np.int64); self.grp[o] = np.repeat(np.arange(len(start)), cnt)
        rank = np.empty(n); rank[o] = np.arange(n) - np.repeat(start, cnt)
        self.z = ndtri((rank + 0.5) / cnt[self.grp])
        self.noise = rng.standard_normal(n)
        self.pick = rng.random(n)
        self.merge = studio_with_one
        # how many of a block group's dots are at most k bedrooms (k = 0..4)
        self.cut = np.floor(cnt[:, None] * cum[gs[start]][:, :SIZES - 1] + rng.random((len(start), 1))).astype(np.int64)
        self.n = n

    def assign(self, rho):
        latent = rho * self.z + np.sqrt(1 - rho * rho) * self.noise
        o = np.lexsort((latent, self.grp))
        gs = self.grp[o]
        start = np.r_[0, np.flatnonzero(gs[1:] != gs[:-1]) + 1]
        cnt = np.diff(np.r_[start, self.n])
        rank = np.empty(self.n, np.int64); rank[o] = np.arange(self.n) - np.repeat(start, cnt)
        if not self.merge:
            return (rank[:, None] >= self.cut[self.grp]).sum(1).astype(np.uint8)
        b = 1 + (rank[:, None] >= self.cut[self.grp][:, 1:]).sum(1)
        small = np.flatnonzero(b == 1)
        o = np.lexsort((self.pick[small], self.grp[small]))
        gs = self.grp[small][o]
        start = np.r_[0, np.flatnonzero(gs[1:] != gs[:-1]) + 1]
        r = np.arange(len(gs)) - np.repeat(start, np.diff(np.r_[start, len(gs)]))
        b[small[o[r < self.cut[gs, 0]]]] = 0
        return b.astype(np.uint8)


def cell_medians(key, beds, price, keep):
    """Median price by (key, bedrooms) over the dots in `keep`, for cells with at least MIN_DOTS dots."""
    k = key[keep].astype(np.int64) * SIZES + beds[keep]
    v = price[keep]
    o = np.lexsort((v, k))
    ks, vs = k[o], v[o]
    start = np.r_[0, np.flatnonzero(ks[1:] != ks[:-1]) + 1]
    cnt = np.diff(np.r_[start, len(ks)])
    lo = vs[start + (cnt - 1) // 2]; hi = vs[start + cnt // 2]
    ok = cnt >= MIN_DOTS
    return pd.Series(np.sqrt(lo * hi)[ok], index=ks[start][ok])


def rent_target(acs, tracts):
    """Tract median gross rent by bedrooms (B25031), below its $3,500 top code; index tract*6 + bedrooms."""
    t = acs['tract'].reindex(tracts)
    R = t[[f'rb{k}' for k in range(SIZES)]].to_numpy()
    R = np.where(R < 3500, R, np.nan)
    i, k = np.nonzero(np.isfinite(R))
    return pd.Series(R[i, k], index=i * SIZES + k)


def value_target(zcta):
    """Zillow home values by bedrooms relative to 3 bedrooms, ZIP codes, 2020-2024 average;
    index zcta*6 + bedrooms (1, 2, 4, 5+)."""
    pos = pd.Series(np.arange(len(zcta)), index=zcta)
    v = {}
    for b in range(1, 6):
        d = pd.read_csv(os.path.join(WORK, 'zillow', f'Zip_zhvi_bdrmcnt_{b}_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv'), dtype={'RegionName': str})
        cols = [c for c in d.columns if c[:4] in ('2020', '2021', '2022', '2023', '2024')]
        v[b] = pd.Series(d[cols].mean(axis=1).to_numpy(), index=d.RegionName.str.zfill(5))
    ratio = {b: (v[b] / v[3]).dropna() for b in (1, 2, 4, 5)}
    out = []
    for b, r in ratio.items():
        r = r[r.index.isin(pos.index)]
        out.append(pd.Series(r.to_numpy(), index=pos.reindex(r.index).to_numpy() * SIZES + b))
    return pd.concat(out)


def fit(order, score):
    """Grid search for the RHO with the smallest score; returns rho, beds, table of scores."""
    rows = []
    for rho in GRID:
        rows.append((rho, *score(order.assign(rho))))
    tab = pd.DataFrame(rows, columns=['rho', 'loss', *[f'bias{k}' for k in range(SIZES)]]).set_index('rho')
    rho = float(tab.loss.idxmin())
    return rho, order.assign(rho), tab


def assign_all(acs, D, tracts, zcta, rng):
    """Bedrooms for all dots. D: concatenated dots (g, t, ref, nc, z, tract). Returns beds (uint8)
    and per tenure the fitted RHO and the table of fit scores."""
    beds = np.zeros(len(D['g']), np.uint8)
    fits = {}
    for tenure in (1, 0):
        cum = shares(acs, tenure)
        sel = np.flatnonzero((D['t'] == tenure) & (D['tract'] % FIT_SHARE == 0))
        if tenure == 1:
            # no-cash renters carry a stand-in rent from a cash rental nearby: they take part in the
            # ordering, but only cash rents are compared with the published medians
            target = rent_target(acs, tracts)
            keep = ~D['nc'][sel]
            def score(b):
                m = cell_medians(D['tract'][sel], b, D['ref'][sel], keep)
                e = np.log(m / target.reindex(m.index)).dropna()
                return (e ** 2).mean(), *e.groupby(e.index % SIZES).mean().reindex(range(SIZES))
        else:
            target = value_target(zcta)
            keep = D['z'][sel] >= 0
            def score(b):
                m = cell_medians(D['z'][sel], b, D['ref'][sel], keep)
                zi, bi = m.index // SIZES, m.index % SIZES
                base = m[bi == 3]; base.index = base.index // SIZES
                r = pd.Series(m.to_numpy() / base.reindex(zi).to_numpy(), index=m.index)[bi != 3]
                e = np.log(r / target.reindex(r.index)).dropna()
                return (e ** 2).mean(), *e.groupby(e.index % SIZES).mean().reindex(range(SIZES))
        rho, _, tab = fit(Ordering(D['g'][sel], D['ref'][sel], cum, rng, studio_with_one=True), score)
        sel = np.flatnonzero(D['t'] == tenure)
        beds[sel] = Ordering(D['g'][sel], D['ref'][sel], cum, rng, studio_with_one=True).assign(rho)
        fits[tenure] = (rho, tab)
    return beds, fits
