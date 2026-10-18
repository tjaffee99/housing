# US Housing Prices, Dot by Dot

An interactive dot density map of what American homes are worth, what they rent for and what they
cost a month, in current dollars. Served at <https://housing.theojaffee.net>.

Every occupied home in the 50 states, DC and Puerto Rico is on the map: 85 million owner-occupied
homes and 45 million rented ones. Zoomed in, one dot is 10 homes; zoomed out, a dot stands for 25,
64, 160 or 400. Prices have no upper limit (the Census Bureau's own figures stop at $2 million and
$3,500 a month).

- **Monthly cost** (the default): every home on one monthly scale: owner-occupied homes by what buying them today
  would cost (20% down, 30-year fixed at the latest Freddie Mac rate, property tax, insurance),
  rented homes by *market rent* (what the home would rent for if it were listed today). *Buying* and
  *Renting* show each group alone; *Rent paid* shows rented homes by what tenants pay now (including
  long-held and rent-regulated leases).
- **Home values**: owner-occupied homes by what they are worth today.
- **Bedrooms** (Studio … 5+): only the homes of that size. Under *Area medians*, ZIP codes colored
  by the typical price of a home with that many bedrooms (Zillow values, HUD Fair Market Rents,
  Census rents paid).
- **Area medians** (Display options): counties, tracts or block groups colored by their median.

A fully static site: no build step for the page, no API keys, no server code.

```
index.html            page shell
style.css             UI styles
app.js                map style, tile routing, legend, hover/click details, search
favicon.svg
vendor/               MapLibre GL JS 5.24.0, PMTiles 4.5.0 and polygon-clipping 0.15.7 (self-hosted)
data/manifest.json    tiers, price classes, vintages and the file index the app loads first
data/summary.json     US, state and county statistics and names (loaded once the map is idle)
data/search.json      states, counties, places and ZIP codes for the search box
data/mask.json        world-minus-US polygons that dim the rest of the base map and its place names
data/dots/t<N><v|p|m|c>.pmtiles   the dots: five nested zoom tiers × value, rent paid, market rent, monthly cost
data/dots/t<N><v|p|m|c><0-5>.pmtiles   the same, one set per bedroom count (studio … 5+), loaded when a size is picked
data/areas/{c,t,b,z}.pmtiles      county, tract, block-group and ZIP outlines with headline medians
data/areas/NNNNN.json full statistics and price distributions for each county's tracts and block groups
pipeline/             Python scripts that build data/ (see below)
.github/workflows/refresh.yml     monthly repricing and deployment
CNAME                 housing.theojaffee.net
```

The base map (roads, water, place names) streams from [OpenFreeMap](https://openfreemap.org),
which is free and needs no key. Map position and mode are kept in the URL (`#value/11.00/40.7300/-73.9600`,
`#cost-paid-areas/...`, `#cost-buying/...`, `#cost-renting-2bd/...`), so links are shareable.

Keyboard: `/` focuses search; with the map focused, arrow keys pan, `+`/`-` zoom and Enter opens
the area under the crosshair; Escape closes the details panel.

## How the prices are made

The base is the Census Bureau's American Community Survey, 2020–2024 5-year estimates, for every
block group: B25075 (home value, 26 brackets), B25063 (gross rent, 24 brackets + no cash rent),
B25003 (tenure), B25042 (tenure by bedrooms), B19013 (income), B25071 (rent burden), B25001/B25002
(units, vacancy), plus, for
tracts and ZIP codes, B25113 (median rent by year moved in), B25031 (median rent by bedrooms) and
B25103 (property taxes).

1. **Dots.** Each block group's homes are drawn bracket by bracket from its own distribution, one dot
   per 10 homes (stochastic rounding keeps totals unbiased). Each dot goes to one of the block
   group's 2020 census blocks in proportion to their housing units, then onto a real building on
   that block (FEMA/ORNL USA Structures, 2023), in proportion to the homes it likely holds: one for
   a house or manufactured home, floor area ÷ 95 m² for an apartment building (height ÷ 3.2 m
   floors). Residential buildings fill a block first; unclassified structures, then non-residential
   ones, take whatever share of the block's 2020 housing units the residential ones can't hold. (In
   dense cities the source often merges a row of houses into one outline and labels it by a school
   or shop in the row, and labels mixed-use apartment buildings commercial; in the suburbs, malls
   and offices sit on blocks whose houses already account for every home, so they stay empty.)
   Renter dots lean toward apartment buildings and owner dots toward houses. On a building of 400 m²
   or more a dot lands anywhere inside its outline, so a rowhouse block fills in along its whole
   length; on a smaller one, within a few meters of its center. Blocks with no mapped building
   (under 1% of dots) get a random point in the block. The buildings are real; the price on a given
   building is not (the Census publishes neighborhood mixes).
2. **A dollar figure for every dot.** Within a bracket, prices are spread evenly (the Census Bureau's
   own assumption, so area medians match the published ones). Above the top codes:
   - *Homes over $2M* take prices from 2024–2025 HMDA purchase records (owner-occupied one-unit homes
     over $2M, restated on the survey's basis with the county's top-tier ZHVI) in the same tract,
     blended with a pool of tracts with a similar share of $2M+ homes in the same county (else state,
     else nation) when the tract has few sales.
   - *Rents over $3,500* follow a Pareto tail fitted to each tract's $2,500–3,000, $3,000–3,500 and
     $3,500+ brackets, shrunk toward its county, state and the nation.
3. **Today's prices.** Values move from the survey years to the latest month with the Zillow Home Value
   Index for the dot's ZIP code (county, state or national where Zillow has no ZIP series): the
   mid-tier index for homes below the ZIP's typical mid-tier home, the top-tier index above its
   typical top-tier home, a log-linear blend between. Rents tenants pay move with the regional CPI
   for rent. In ZIP codes where most homes are over $2M, the excess over the top code is scaled so
   the ZIP's median matches Zillow's typical value (relative to ZIPs the survey fully describes).
4. **Market rents.** In each tract, households that moved in since 2023 pay M times the tract's overall
   median (B25113; estimated by empirical Bayes, since most tract-to-tract spread in the published
   figures is sampling noise). A rental's market rent is its rent times M^(2(1−q)), q its rank among
   the tract's rents: the cheapest, longest-held homes gain the most, the dearest nothing. These are
   restated with Zillow's Observed Rent Index (ZORI), and where ZORI covers a ZIP code, market rents
   are raised (through the same rank-weighted uplift, at most 1.6×) until the ZIP's median reaches
   ZORI. ZORI excludes utilities, so it is a floor, not a target.
5. **Monthly cost.** Owners: what buying the home today would cost a month: 20% down, 30-year fixed at the
   latest Freddie Mac weekly rate, property tax at the tract's effective rate (median tax ÷ median
   value; at least 1.1% in California, where a sale resets the tax base), insurance 0.5% of value a
   year. Renters: market rent.
6. **Bedrooms.** Every dot has a size (studio, 1–4, 5+). B25042 gives each block group's homes by
   tenure and bedrooms, not prices by bedrooms, so a block group's dots of one tenure are ordered by
   ρ·z + √(1−ρ²)·noise, z the normal score of the dot's price rank there, and take sizes smallest
   first, with counts that match the block group's (systematic rounding). Studios share the
   1-bedroom place and are picked at random among those dots: the cheapest rents in a neighborhood
   are more often long-held larger homes than studios. ρ is fitted per tenure on every third tract:
   for renters, so that tract medians of rent by bedrooms match B25031 (ρ ≈ 0.6, every size within
   about 7% on average); for owners, so that ZIP-code ratios between sizes match Zillow's ZHVI by
   bedroom count over 2020–2024 (ρ ≈ 0.4). `pipeline/beds.py`.
7. **Sizes by ZIP code** (*Area medians* with a size): Zillow's ZHVI by bedroom count, HUD's Small
   Area Fair Market Rents (county FMRs outside metro areas), the Census median rent paid by bedrooms
   (in today's dollars), and the monthly cost of buying Zillow's typical home of each size.

Area statistics (medians and class shares for every block group, tract, county, state, ZIP code and
the nation) come from the same distributions, integrated exactly rather than from the dots.

**Known gaps.** The 2024 ACS release has no rows for 39 block groups in Suffolk County, NY (most of
Brentwood and Wyandanch); `01_acs.py` gives them the county total minus the published block groups,
split by 2020 housing units, and flags them as estimates. Prices far above the top codes are
modeled; block-group figures have wide margins of error.

| tier | 1 dot = | shown at zoom | tiles stored at |
|------|---------|---------------|-----------------|
| t1   | 400 homes | 0–6   | z0–4 |
| t2   | 160 homes | 6–8   | z5 |
| t3   | 64 homes  | 8–10  | z7 |
| t4   | 25 homes  | 10–12 | z9 |
| t5   | 10 homes  | 12+   | z11 |

The coarser tiers are random subsets of the finest one (stratified within each block group), so
zooming in only adds dots; tiers cross-fade at their boundaries. Each mode is a separate archive set,
so only the mode on screen downloads, and any archive over 45 MB is split into tile-column shards
that `app.js` routes requests to (GitHub's per-file limit is 100 MB).

## Run locally

The generated tiles and statistics (`data/dots`, `data/areas`, `data/summary.json`,
`data/manifest.json`) are not committed: the refresh workflow builds them for each deploy. To run
the site locally, build them (see below; with the `base` branch's kit, steps 04–06 and 09 are
enough) or download the latest Pages artifact from the Actions tab into `data/`. PMTiles needs HTTP
range requests, which `python3 -m http.server` does not support. Use, e.g.:

```
npx http-server . -p 8000     # then open http://localhost:8000
```

## Rebuilding the data

Needs Python 3.11+ with `numpy pandas geopandas pyogrio shapely pmtiles openpyxl`, and
[tippecanoe](https://github.com/felt/tippecanoe) on the PATH (or `TIPPECANOE=/path/to/tippecanoe`).
Downloads (~12 GB, mostly census blocks) go in `pipeline/work/` (or `$HOUSING_WORK`), which is not
published. From `pipeline/`:

```
python3 tools/fetch_sources.py --all     # Zillow, FRED (CPI, mortgage rates), Census outlines, ACS tables, HUD
python3 tools/fetch_hmda.py 2024 2025    # HMDA purchase records (~45 MB after trimming)
python3 tools/fetch_structures.py        # USA Structures: every building (~45 GB streamed, ~5 GB kept)
# census blocks: work/blocks/<SS>.zip from
#   https://www2.census.gov/geo/tiger/TIGER2024/TABBLOCK20/tl_2024_<SS>_tabblock20.zip  (each state + DC + PR)

python3 01_acs.py        # parse ACS tables (1 min)
PROCS=2 python3 02_dots.py   # 13 million dots placed on buildings (~20 min; ~5 GB of memory per process)
python3 03_base.py       # reference prices per dot, uncapped; market rents; bedrooms (4 min)
python3 04_prices.py     # today's prices, monthly costs, area statistics (5 min)
python3 05_tiles.py      # dot tiles, four modes, all sizes and each bedroom count (6 min)
python3 06_areas.py      # outlines, detail files, summary (8 min)
python3 07_search.py     # search index (3 min)
python3 08_mask.py       # data/mask.json
python3 09_manifest.py   # data/manifest.json
python3 tools/kit.py pack ../../kit    # base data for the monthly refresh (see below)
```

Random seeds are fixed, so a rebuild from the same inputs gives the same dots.

## Monthly refresh

`.github/workflows/refresh.yml` runs on the 20th of each month (after Zillow and the BLS publish),
on every push to `main`, and by hand from the Actions tab. It reads the slow-changing base data
(dot positions, reference prices, slimmed Census tables, HUD rents; about 225 MB) from the `base`
branch, downloads the latest Zillow indexes, CPI and mortgage rate and the Census outlines, runs
steps 04, 05, 06 and 09, and deploys the site to GitHub Pages. It needs
**Settings → Pages → Build and deployment → Source: GitHub Actions**; until that is set, the
workflow builds and skips the deploy, and Pages keeps serving `main` as committed.

After a new ACS release (each December), rerun the full build and push a new kit to `base`:

```
python3 tools/kit.py pack ../../kit
cd ../../kit && git init -b base && git add -A && git commit -m "Base data" &&
  git push -f https://github.com/<you>/housing base
```

## Deploying on the subdomain

GitHub Pages allows one custom domain per repository; this repository's `CNAME` is
`housing.theojaffee.net`, with a DNS record `CNAME housing → tjaffee99.github.io` and "Enforce HTTPS"
on. Any other static host that supports HTTP range requests works too (Cloudflare Pages, Netlify,
Vercel, S3/R2): serve the repository root with no build command.

## Sources and licenses

US Census Bureau (ACS 2020–2024, TIGER/Line, cartographic boundaries; public domain). Zillow Research:
ZHVI and ZORI (free to use with attribution). HUD Fair Market Rents FY2027. CFPB/FFIEC HMDA
2024–2025. Freddie Mac PMMS and BLS CPI via FRED. FEMA/ORNL USA Structures (public domain). MapLibre GL JS (BSD-3-Clause,
`vendor/MAPLIBRE-LICENSE.txt`), PMTiles (BSD-3-Clause), polygon-clipping (MIT, `vendor/POLYGON-CLIPPING-LICENSE.md`). Base map © OpenStreetMap contributors
(ODbL), tiles by OpenFreeMap (OpenMapTiles schema).
