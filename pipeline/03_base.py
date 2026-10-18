"""Reference prices: a continuous dollar figure for every dot and for every block group's price
distribution, on the ACS basis (2020-2024 answers in 2024 dollars), with no top code.
04_prices.py restates them at current prices.

Within a bracket, prices are spread evenly (the Census Bureau's own assumption when it interpolates
medians from these brackets, so area medians match the published ones). Above the ACS top codes:

  Home values over $2M   drawn from 2024-2025 HMDA purchase prices over $2M (owner-occupied, one-unit
                         homes, restated on the ACS basis with the county's top-tier ZHVI) in the
                         tract, blended with a pool of similar tracts (same county, or else state or
                         nation, with a similar share of $2M+ homes) when the tract has few sales.
  Rents over $3,500      Pareto tail fitted to the tract's own $2,500-3,000, $3,000-3,500 and
                         $3,500+ brackets (grouped likelihood), shrunk toward its county, state and
                         the nation.

Market rents: what a home would rent for if it were let today. A tract's recent movers (moved in
2023 or later, ACS B25113) pay M times the tract's overall median rent; M is estimated per tract by
empirical Bayes (the tract's own 2023+ and 2020-22 medians, shrunk toward the county's typical
within-tract ratio, since most tract-to-tract spread in the published figures is sampling noise).
Each rental's market rent is its paid rent times M^(2(1-q)), q its rank among the tract's rents: the
cheapest (longest-held, rent-regulated) homes gain M^2, the median M, the dearest nothing.

Inputs  work/acs.pkl, work/dots/*.npz, work/hmda/*.csv.gz, work/geo/cb_zcta.zip, work/zillow (county top-tier ZHVI)
Bedrooms: see beds.py.

Output  work/base/<state>.npz   dots: x y t u g (as 02_dots) + z (ZCTA index), ref (value or paid rent),
                                mref (market rent), q (rank among the tract's rents), nc (no cash rent),
                                beds (0 studio .. 5 five or more)
        work/base/points.npz    block-group quadrature points for area statistics: g t nc ref mref q w
        work/base/meta.pkl      ZCTA codes, block group -> main ZCTA, tract market-rent ratios, tax rates
"""
import os, sys, glob, pickle, time
import multiprocessing as mp
import numpy as np, pandas as pd, geopandas as gpd, shapely
import econ, beds
from common import WORK, STATES, VALUE_EDGES, RENT_EDGES, ACS_YEAR

BASE = os.path.join(WORK, 'base')
Q = 6                    # quadrature points per (block group, bracket)
Q_TOP = 24               # ... in the open top brackets, so area shares reach the far tail
K_SALES = 10             # weight (in sales) of the similar-tract pool in a tract's value tail
POOL_MIN = 30            # sales a pool needs before it is used
POOL_MAX = 200           # sales kept per tract and per pool (random subsample)
SHARE_EDGES = [0, .02, .05, .1, .2, .35, .5, .7, .85, 1.01]   # tract share of homes worth $2M+
K_RENT = 150             # prior weight (homes) for the rent tail
VALUE_TOP, RENT_TOP = 2_000_000.0, 3_500.0
RENT_CAP = 30_000.0      # monthly rent ceiling for the Pareto draw


# ---------------------------------------------------------------------------------- brackets
def pareto(L, alpha, U, u):
    """Inverse CDF of a Pareto(L, alpha) truncated at U."""
    return L * (1.0 - u * (1.0 - (L / U) ** alpha)) ** (-1.0 / alpha)


class Tables:
    """Per-group empirical distributions (sorted log values with weights), sampled by inverse CDF
    with linear interpolation between weighted midpoints. All groups share flat arrays; a group's
    CDF positions are offset by its index so one searchsorted serves every group."""
    def __init__(self, groups, n_groups):
        vals, pos, start, cnt = [], [], np.zeros(n_groups, np.int64), np.zeros(n_groups, np.int64)
        off = 0
        for gi in range(n_groups):
            v, w = groups.get(gi, (None, None))
            if v is None or not len(v):
                start[gi] = off; continue
            o = np.argsort(v)
            v, w = v[o], w[o] / w.sum()
            c = np.cumsum(w) - w / 2
            vals.append(v); pos.append(gi + c)
            start[gi], cnt[gi] = off, len(v)
            off += len(v)
        self.v = np.concatenate(vals) if vals else np.zeros(0)
        self.p = np.concatenate(pos) if pos else np.zeros(0)
        self.start, self.cnt = start, cnt

    def sample(self, gi, u):
        gi = np.asarray(gi, np.int64)
        assert (self.cnt[gi] > 0).all(), 'value tail: a group without a table'
        target = gi + u
        j = np.searchsorted(self.p, target)
        first = self.start[gi]; last = first + self.cnt[gi] - 1
        jl = np.clip(j - 1, first, last); jr = np.clip(j, first, last)
        span = self.p[jr] - self.p[jl]
        f = np.where(span > 0, np.clip((target - self.p[jl]) / np.where(span > 0, span, 1), 0, 1), 0)
        return np.exp(self.v[jl] + f * (self.v[jr] - self.v[jl]))


def draw(bins, tract, u, edges, top_draw):
    """Reference dollar figure for each item in bracket `bins`: even spread inside finite brackets,
    top_draw(tract, u) in the open top bracket (len(edges)-1)."""
    e = np.asarray(edges, float)
    nf = len(e) - 1
    out = np.full(len(bins), np.nan)
    fin = bins < nf
    b = bins[fin]
    out[fin] = e[b] + u[fin] * (e[b + 1] - e[b])
    t = bins == nf
    if t.any():
        out[t] = top_draw(tract[t], u[t])
    return out


# ---------------------------------------------------------------------------------- tails
def hmda():
    rows = []
    for f in sorted(glob.glob(os.path.join(WORK, 'hmda', '*.csv.gz'))):
        year = int(os.path.basename(f)[:4])
        d = pd.read_csv(f, dtype=str)
        d = d[(d.occupancy_type == '1') & (d.total_units == '1') & d.derived_dwelling_category.str.endswith('Site-Built', na=False)]
        d = d.assign(v=pd.to_numeric(d.property_value, errors='coerce'), year=year)
        rows.append(d[(d.v >= 10_000) & (d.v <= 250_000_000) & d.census_tract.str.len().eq(11)][['census_tract', 'v', 'year']])
    h = pd.concat(rows)
    print(f'HMDA: {len(h):,} owner-occupied one-unit purchases')
    # restate on the ACS basis: county top-tier ZHVI 2020-2024 reference / the sale year's average
    zt = econ.zillow('County_zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month.csv', 'county')
    ref, _, _ = econ.ref_and_now(zt, (ACS_YEAR - 4, ACS_YEAR), 36)
    county = h.census_tract.str[:5].to_numpy()
    k = np.full(len(h), np.nan)
    for y in sorted(h.year.unique()):
        r = ref / econ.year_mean(zt, y)
        sel = (h.year == y).to_numpy()
        k[sel] = r.reindex(county[sel]).to_numpy()
        miss = sel & np.isnan(k)          # no county index: the state's median, else the nation's
        k[miss] = r.groupby(r.index.str[:2]).median().reindex(pd.Index(county[miss]).str[:2]).to_numpy()
        miss = sel & np.isnan(k)
        k[miss] = r.median()
    h['vr'] = h.v.to_numpy() * k
    return h


def value_tail(acs):
    """Distribution of home values over $2M for every tract with such homes: the tract's own HMDA
    sales over $2M, plus (weight K_SALES sales) a pool of sales from tracts with a similar share of
    $2M+ homes in the same county, else state, else nation."""
    rng = np.random.default_rng(2)
    h = hmda()
    tail = h[h.vr >= VALUE_TOP]
    tr = acs['tract']
    tracts = tr.index
    share = (tr.v25 / tr.val_total).fillna(0).to_numpy()
    bucket = np.clip(np.searchsorted(SHARE_EDGES, share, side='right') - 1, 0, len(SHARE_EDGES) - 2)
    tb = pd.Series(bucket, index=tracts)
    tpos = pd.Series(np.arange(len(tracts)), index=tracts)
    t_idx = tpos.reindex(tail.census_tract).to_numpy()
    ok = np.isfinite(t_idx)
    lv = np.log(tail.vr.to_numpy()[ok]); ti = t_idx[ok].astype(np.int64)
    print(f'HMDA: {len(tail):,} purchases over $2M on the ACS basis, {ok.sum():,} in known tracts '
          f'(max ${tail.vr.max() / 1e6:.0f}M)')
    tb_arr = bucket[ti]
    co = pd.Index(tracts[ti]).str[:5].to_numpy(); stt = pd.Index(tracts[ti]).str[:2].to_numpy()
    def groups(keys):
        df = pd.DataFrame({'k': keys, 'i': np.arange(len(keys))})
        return {k: g.i.to_numpy() for k, g in df.groupby('k')}
    own = groups(ti)
    pc = groups(pd.Series(co).str.cat(tb_arr.astype(str), sep='|').to_numpy())
    ps = groups(pd.Series(stt).str.cat(tb_arr.astype(str), sep='|').to_numpy())
    pn = groups(tb_arr)
    def sub(ix):
        return ix if len(ix) <= POOL_MAX else rng.choice(ix, POOL_MAX, replace=False)
    need = np.flatnonzero(tr.v25.fillna(0).to_numpy() > 0)
    tables, src = {}, {'tract': 0, 'county': 0, 'state': 0, 'nation': 0}
    for i in need:
        b_ = int(bucket[i]); c_ = tracts[i][:5]; s_ = tracts[i][:2]
        mine = own.get(i, np.zeros(0, np.int64))
        for name, pool in (('county', pc.get(f'{c_}|{b_}')), ('state', ps.get(f'{s_}|{b_}')), ('nation', pn.get(b_))):
            if pool is not None and len(pool) >= POOL_MIN: break
        pool = np.setdiff1d(pool, mine)
        w_own = len(mine) / (len(mine) + K_SALES)
        m_, p_ = sub(mine), sub(pool)
        v = np.r_[lv[m_], lv[p_]]
        w = np.r_[np.full(len(m_), w_own / max(len(m_), 1)), np.full(len(p_), (1 - w_own) / max(len(p_), 1))]
        tables[i] = (v, w)
        src['tract' if w_own >= 0.5 else name] += 1
    T = Tables(tables, len(tracts))
    print(f'value tail: {len(need):,} tracts with $2M+ homes; mostly own sales {src["tract"]:,}, '
          f'pooled by county {src["county"]:,}, state {src["state"]:,}, nation {src["nation"]:,}')
    return T, {'n_tail': int(len(tail)), 'sources': src}


RENT_GRID = np.arange(0.6, 10.0, 0.02)


def rent_tail(acs):
    """Pareto shape per tract for rents over $3,500, fitted to the $2,500-3,000, $3,000-3,500 and
    $3,500+ brackets (grouped likelihood, Pareto from $2,500), shrunk toward county, state, nation."""
    x = np.array([2500.0, 3000.0, 3500.0])
    A = RENT_GRID[:, None]
    surv = (x[None, :] / 2500.0) ** -A                 # P(X >= 2500, 3000, 3500)
    P = np.stack([surv[:, 0] - surv[:, 1], surv[:, 1] - surv[:, 2], surv[:, 2]], 1)   # (grid, 3)
    logP = np.log(P)
    def fit(counts, prior_alpha):
        if prior_alpha is not None:
            j = np.clip(np.searchsorted(RENT_GRID, prior_alpha), 0, len(RENT_GRID) - 1)
            counts = counts + K_RENT * P[j]
        ll = counts @ logP.T                           # (n, grid)
        return RENT_GRID[np.argmax(ll, axis=1)]
    cols = ['r21', 'r22', 'r23']
    a_us = fit(acs['us'][cols].to_numpy(float), None)[0]
    st = acs['state']
    a_st = pd.Series(fit(st[cols].fillna(0).to_numpy(float), np.full(len(st), a_us)), index=st.index)
    co = acs['county']
    a_co = pd.Series(fit(co[cols].fillna(0).to_numpy(float), a_st.reindex(co.index.str[:2]).fillna(a_us).to_numpy()), index=co.index)
    tr = acs['tract']
    a_tr = pd.Series(fit(tr[cols].fillna(0).to_numpy(float), a_co.reindex(tr.index.str[:5]).fillna(a_us).to_numpy()), index=tr.index)
    # where density still rises past $3,500 a Pareto from $2,500 fits a near-flat tail; a floor of 2
    # keeps such tracts from putting a fifth of their top bracket above $10,000
    a_tr = a_tr.clip(2.0, 8.0)
    print(f'rent tail: national alpha {a_us:.2f}; tract alphas {np.percentile(a_tr, [5, 50, 95]).round(2)}')
    return a_tr, {'alpha_us': float(a_us)}


# ---------------------------------------------------------------------------------- market rents
def market_ratio(acs):
    """Empirical-Bayes ratio of recent movers' median rent to the tract's overall median."""
    t = acs['tract']
    def obs(med, moe):
        # medians at the $3,501 top code say nothing about the ratio
        y = np.log(t[med] / t.rm_all)
        se = (t[moe] / 1.645) / t[med]
        ok = np.isfinite(y) & (se > 0) & (t[med] < 3501) & (t.rm_all < 3501)
        return y.where(ok), (se ** 2).where(ok)
    y1, v1 = obs('rm_2023', 'rm_2023_moe')
    y0, v0 = obs('rm_2020', 'rm_2020_moe')
    # 2020-22 movers pay a bit less than 2023+ movers: shift by the state's typical gap
    gap = (y1 - y0).groupby(t.index.str[:2]).median()
    us_gap = float((y1 - y0).median())
    y0 = y0 + gap.reindex(t.index.str[:2]).fillna(us_gap).to_numpy()
    v0 = v0 + 0.01
    # between-tract variance of the true ratio: total variance minus average sampling variance
    tau2 = max(float(y1.var() - v1.mean()), 0.004)
    # county prior: precision-weighted mean of its tracts' ratios, shrunk toward the state's
    def wmean(y, v, key):
        w = 1.0 / (v + tau2)
        d = pd.DataFrame({'wy': (w * y), 'w': w}).dropna()
        g = d.groupby(key.reindex(d.index))
        return g.wy.sum(), g.w.sum()
    st_key = pd.Series(t.index.str[:2], index=t.index)
    co_key = pd.Series(t.index.str[:5], index=t.index)
    swy, sw = wmean(y1, v1, st_key)
    us_mu = float((swy.sum()) / sw.sum())
    st_mu = (swy + 20 * us_mu) / (sw + 20)
    cwy, cw = wmean(y1, v1, co_key)
    prior_st = st_mu.reindex(cw.index.str[:2]).fillna(us_mu).to_numpy()
    co_mu = (cwy + 20 * prior_st) / (cw + 20)
    mu = co_mu.reindex(t.index.str[:5]).to_numpy()
    mu = np.where(np.isfinite(mu), mu, st_mu.reindex(t.index.str[:2]).fillna(us_mu).to_numpy())
    num = mu / tau2
    den = np.full(len(t), 1.0 / tau2)
    for y, v in ((y1, v1), (y0, v0)):
        ok = y.notna().to_numpy()
        num[ok] += (y / v).to_numpy()[ok]
        den[ok] += (1 / v).to_numpy()[ok]
    M = pd.Series(np.clip(np.exp(num / den), 0.9, 2.5), index=t.index)
    print(f'market ratio: tau {np.sqrt(tau2):.3f}, US prior {np.exp(us_mu):.3f}; tract M {np.percentile(M, [5, 25, 50, 75, 95]).round(3)}')
    return M


# ---------------------------------------------------------------------------------- per state
_G = {}


def init_worker():
    z = gpd.read_file(os.path.join(WORK, 'geo', 'cb_zcta.zip'), columns=['ZCTA5CE20']).to_crs(4326)
    _G['zgeom'] = np.asarray(z.geometry.values)
    _G['ztree'] = shapely.STRtree(_G['zgeom'])
    _G['shared'] = pickle.load(open(os.path.join(BASE, 'shared.pkl'), 'rb'))


def lonlat(x, y):
    n = float(1 << 24)
    return (x + 0.5) / n * 360 - 180, np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + 0.5) / n))))


def assign_zcta(lon, lat):
    pts = shapely.points(lon, lat)
    z = np.full(len(pts), -1, np.int32)
    i, j = _G['ztree'].query(pts, predicate='within')
    z[i] = j
    miss = np.flatnonzero(z < 0)
    if len(miss):
        i, j = _G['ztree'].query_nearest(pts[miss], max_distance=0.03, all_matches=False)
        z[miss[i]] = j
    return z


def top_draws(S):
    return (lambda tr, u: S['vtail'].sample(tr, u),
            lambda tr, u: pareto(RENT_TOP, S['ralpha'][tr], RENT_CAP, u))


def process(st):
    t0 = time.time()
    S = _G['shared']
    vtop, rtop = top_draws(S)
    d = dict(np.load(os.path.join(WORK, 'dots', st + '.npz')))
    rng = np.random.default_rng(31000 + int(st))
    n = len(d['x'])
    lon, lat = lonlat(d['x'].astype(float), d['y'].astype(float))
    z = assign_zcta(lon, lat)
    g = d['g'].astype(np.int64)
    county = S['bg_county'][g]; tract = S['bg_tract'][g]
    t, b = d['t'], d['b'].astype(np.int64)
    ref = np.full(n, np.nan); mref = np.full(n, np.nan)
    u = rng.random(n)
    own = t == 0
    ref[own] = draw(b[own], tract[own], u[own], VALUE_EDGES, vtop)
    ren = np.flatnonzero(t == 1)
    nc = np.zeros(n, bool)
    cash = ren[b[ren] < 24]
    ref[cash] = draw(b[cash], tract[cash], u[cash], RENT_EDGES, rtop)
    # rank of each cash rent within its tract (random tie-break)
    o = np.lexsort((rng.random(len(cash)), ref[cash], tract[cash]))
    cs = cash[o]
    tr_s = tract[cs]
    start = np.r_[0, np.flatnonzero(tr_s[1:] != tr_s[:-1]) + 1]
    cnt = np.diff(np.r_[start, len(cs)])
    grp = np.repeat(np.arange(len(start)), cnt)
    rank = np.arange(len(cs)) - start[grp]
    q = np.full(n, np.nan)
    q[cs] = (rank + 0.5) / cnt[grp]
    # no cash rent: a random cash rental from the same tract stands in (else the county's, else the state's)
    ncs = ren[b[ren] == 24]
    nc[ncs] = True
    for key in (tract, county, np.zeros(n, np.int64)):
        need = ncs[np.isnan(ref[ncs])]
        if not len(need) or not len(cash): break
        o2 = np.argsort(key[cash], kind='stable')
        ks = key[cash][o2]
        lo_ = np.searchsorted(ks, key[need]); hi_ = np.searchsorted(ks, key[need], 'right')
        has = hi_ > lo_
        j = cash[o2][(lo_ + (rng.random(len(need)) * (hi_ - lo_)).astype(np.int64))[has]]
        ref[need[has]] = ref[j]; q[need[has]] = q[j]
    M = S['mratio'][tract[ren]]
    mref[ren] = ref[ren] * M ** (2 * (1 - q[ren]))
    out = {k: d[k] for k in ('x', 'y', 't', 'u', 'g')}
    out.update(z=z, ref=ref.astype(np.float32), mref=mref.astype(np.float32), q=q.astype(np.float32), nc=nc)
    np.savez(os.path.join(BASE, st + '.npz'), **out)
    print(f'{st}: {n:,} dots, {np.mean(z < 0) * 100:.2f}% outside ZCTAs, {time.time() - t0:.0f}s', flush=True)
    return st, z, g


# ---------------------------------------------------------------------------------- area points
def points(acs, S):
    """Q quadrature points per block group and bracket (owners, cash rents, no-cash rents)."""
    bg = acs['bg']
    county = S['bg_county']; tract = S['bg_tract']
    V = bg[[f'v{i}' for i in range(26)]].fillna(0).to_numpy()
    R = bg[[f'r{i}' for i in range(25)]].fillna(0).to_numpy()
    qk = (np.arange(Q) + 0.5) / Q
    vtop, rtop = top_draws(S)
    def expand(C, cols, top):
        # Q points per bracket (Q_TOP in the open top bracket `top`) at the bracket's even quantiles
        gi, bi = np.nonzero(C[:, cols] > 0)
        bi = np.asarray(cols)[bi]
        nq = np.where(bi == top, Q_TOP, Q)
        k = np.arange(nq.sum()) - np.repeat(np.cumsum(nq) - nq, nq)
        return (np.repeat(gi, nq), np.repeat(bi, nq), np.repeat(C[gi, bi] / nq, nq), (k + 0.5) / np.repeat(nq, nq))
    g, b, w, uq = expand(V, list(range(26)), 25)
    ref = draw(b, tract[g], uq, VALUE_EDGES, vtop)
    own = dict(g=g, t=np.zeros(len(g), np.uint8), nc=np.zeros(len(g), bool), ref=ref, mref=np.full(len(g), np.nan),
               q=np.full(len(g), np.nan), w=w)
    g, b, w, uq = expand(R, list(range(24)), 23)
    ref = draw(b, tract[g], uq, RENT_EDGES, rtop)
    # weighted rank within tract
    tr = tract[g]
    o = np.lexsort((ref, tr))
    g, b, w, ref, tr = g[o], b[o], w[o], ref[o], tr[o]
    start = np.r_[0, np.flatnonzero(tr[1:] != tr[:-1]) + 1]
    cnt = np.diff(np.r_[start, len(tr)])
    grp = np.repeat(np.arange(len(start)), cnt)
    cw = np.cumsum(w)
    base = np.r_[0, cw][start][grp]
    tot = np.add.reduceat(w, start)[grp]
    q = (cw - base - w / 2) / tot
    mref = ref * S['mratio'][tr] ** (2 * (1 - q))
    cash = dict(g=g, t=np.ones(len(g), np.uint8), nc=np.zeros(len(g), bool), ref=ref, mref=mref, q=q, w=w)
    # no cash rent: Q points at the tract's cash-rent quantiles (else the county's, else the state's)
    gi = np.flatnonzero(R[:, 24] > 0)
    g2 = np.repeat(gi, Q); q2 = np.tile(qk, len(gi)); w2 = np.repeat(R[gi, 24] / Q, Q)
    ref2 = np.full(len(g2), np.nan)
    def fill(key_pts, key_need):
        # weighted quantile q2 of the cash points sharing a key, vectorized: within-group CDF
        # positions are offset by the group number so one searchsorted serves every group
        need = np.flatnonzero(np.isnan(ref2))
        o = np.lexsort((ref, key_pts))
        kp, rp, wp = key_pts[o], ref[o], w[o]
        st_ = np.r_[0, np.flatnonzero(kp[1:] != kp[:-1]) + 1]
        cnt_ = np.diff(np.r_[st_, len(kp)])
        gi_ = np.repeat(np.arange(len(st_)), cnt_)
        c = np.cumsum(wp); c0 = np.r_[0, c][st_][gi_]; tot_ = (np.r_[0, c][np.r_[st_[1:], len(kp)]] - np.r_[0, c][st_])[gi_]
        pos = gi_ + (c - c0 - wp / 2) / tot_
        ukeys = kp[st_]
        k = np.searchsorted(ukeys, key_need[need])
        k = np.clip(k, 0, len(ukeys) - 1)
        has = ukeys[k] == key_need[need]
        need, k = need[has], k[has]
        target = k + q2[need]
        j = np.clip(np.searchsorted(pos, target), 1, len(pos) - 1)
        # clamp to the group's own points
        first = st_[k]; last = first + cnt_[k] - 1
        jl = np.clip(j - 1, first, last); jr = np.clip(j, first, last)
        span = pos[jr] - pos[jl]
        f = np.where(span > 0, np.clip((target - pos[jl]) / np.where(span > 0, span, 1), 0, 1), 0)
        ref2[need] = rp[jl] + f * (rp[jr] - rp[jl])
    fill(tr, tract[g2])
    fill(county[g], county[g2])
    fill(S['bg_state'][g], S['bg_state'][g2])
    mref2 = ref2 * S['mratio'][tract[g2]] ** (2 * (1 - q2))
    noc = dict(g=g2, t=np.ones(len(g2), np.uint8), nc=np.ones(len(g2), bool), ref=ref2, mref=mref2, q=q2, w=w2)
    allp = {k: np.concatenate([own[k], cash[k], noc[k]]) for k in own}
    o = np.argsort(allp['g'], kind='stable')
    return {k: (v[o].astype(np.float32) if v.dtype == np.float64 else v[o].astype(np.int32) if k == 'g' else v[o])
            for k, v in allp.items()}


def main():
    os.makedirs(BASE, exist_ok=True)
    acs = pickle.load(open(os.path.join(WORK, 'acs.pkl'), 'rb'))
    bg_ids = acs['bg'].index
    counties = acs['county'].index; tracts = acs['tract'].index
    cpos = pd.Series(np.arange(len(counties)), index=counties)
    tpos = pd.Series(np.arange(len(tracts)), index=tracts)
    bg_county = cpos.reindex(bg_ids.str[:5]).to_numpy()
    bg_tract = tpos.reindex(bg_ids.str[:11]).to_numpy()
    assert not np.isnan(bg_county).any() and not np.isnan(bg_tract).any()
    vtail, vdiag = value_tail(acs)
    ra, rdiag = rent_tail(acs)
    M = market_ratio(acs)
    S = dict(bg_county=bg_county.astype(np.int64), bg_tract=bg_tract.astype(np.int64),
             bg_state=np.array([int(x) for x in bg_ids.str[:2]]), vtail=vtail,
             ralpha=ra.reindex(tracts).to_numpy(), mratio=M.reindex(tracts).fillna(1.0).to_numpy())
    pickle.dump(S, open(os.path.join(BASE, 'shared.pkl'), 'wb'))

    states = sys.argv[1:] or STATES
    states = sorted(states, key=lambda s: -os.path.getsize(os.path.join(WORK, 'dots', s + '.npz')))
    with mp.Pool(int(os.environ.get('PROCS', 4)), initializer=init_worker) as pool:
        res = pool.map(process, states, chunksize=1)
    # each block group's main ZCTA (most of its dots)
    z_all = np.concatenate([r[1] for r in res]); g_all = np.concatenate([r[2] for r in res])
    ok = z_all >= 0
    pair = pd.DataFrame({'g': g_all[ok], 'z': z_all[ok]}).value_counts().reset_index()
    pair = pair.sort_values(['g', 'count'], ascending=[True, False]).drop_duplicates('g')
    bg_zcta = np.full(len(bg_ids), -1, np.int32)
    bg_zcta[pair.g.to_numpy()] = pair.z.to_numpy()
    zc = gpd.read_file(os.path.join(WORK, 'geo', 'cb_zcta.zip'), columns=['ZCTA5CE20'], ignore_geometry=True).ZCTA5CE20.to_numpy()

    t0 = time.time()
    P = points(acs, S)
    np.savez(os.path.join(BASE, 'points.npz'), **P)
    print(f'{len(P["g"]):,} area points ({time.time() - t0:.0f}s)')
    # bedrooms, fitted over every state's dots at once
    t0 = time.time()
    parts = {st: dict(np.load(os.path.join(BASE, st + '.npz'))) for st in STATES}
    D = {k: np.concatenate([parts[st][k] for st in STATES]) for k in ('g', 't', 'ref', 'nc', 'z')}
    D['g'] = D['g'].astype(np.int64); D['tract'] = S['bg_tract'][D['g']]
    b, fits = beds.assign_all(acs, D, np.asarray(tracts), zc, np.random.default_rng(41))
    off = 0
    for st in STATES:
        n = len(parts[st]['g'])
        parts[st]['beds'] = b[off:off + n]; off += n
        np.savez(os.path.join(BASE, st + '.npz'), **parts[st])
    for t, (rho, tab) in fits.items():
        mix = np.bincount(b[D['t'] == t], minlength=beds.SIZES) / max((D['t'] == t).sum(), 1)
        print(f'bedrooms, {["owners", "renters"][t]}: rho {rho:.2f}, mean log error by size '
              f'{np.round(tab.loc[rho].to_numpy()[1:], 3).tolist()}, mix {np.round(mix, 3).tolist()}')
    print(f'bedrooms assigned ({time.time() - t0:.0f}s)')
    meta = dict(zcta=zc, bg_zcta=bg_zcta, bg=np.asarray(bg_ids), counties=np.asarray(counties), tracts=np.asarray(tracts),
                mratio=M, ralpha=ra, diag=dict(value=vdiag, rent=rdiag, beds={t: f for t, f in fits.items()}))
    pickle.dump(meta, open(os.path.join(BASE, 'meta.pkl'), 'wb'))


if __name__ == '__main__':
    main()
