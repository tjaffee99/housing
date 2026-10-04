# US Housing Prices, Dot by Dot

An interactive dot density map of what American homes are worth and what they rent for.
Intended to be served at <https://housing.theojaffee.net>.

Every occupied home in the 50 states, DC and Puerto Rico is on the map: 85 million
owner-occupied homes colored by value and 45 million rented homes colored by monthly gross
rent. Zoomed in, one dot is 10 homes; zoomed out, a dot stands for 25, 64, 160 or 400.

A fully static site: no build step, no API keys, no server code.

```
index.html            page shell
style.css             UI styles
app.js                map style, tile routing, legend, hover/click details, search
favicon.svg
vendor/               MapLibre GL JS 5.24.0 and PMTiles 4.5.0 (self-hosted)
data/manifest.json    tiers, price classes and file index the app loads first
data/summary.json     US / state / county statistics and county names
data/search.json      states, counties, places and ZIP codes for the search box
data/mask.json        world-minus-US polygons that dim the rest of the base map
data/us.json          coarse US outline (foreign place names are drawn dimmer)
data/dots/t<N><v|r>*.pmtiles  the dots: five nested zoom tiers, home values (v) and rents (r)
data/areas/*.pmtiles  county, tract and block-group outlines with headline stats
data/areas/NNNNN.json full stats and price distributions for each county's tracts and block groups
pipeline/             Python scripts that build data/ from Census downloads
CNAME                 housing.theojaffee.net
```

The base map (roads, water, place names) streams from [OpenFreeMap](https://openfreemap.org),
which is free and needs no key. Map position and mode are kept in the URL
(`#value/11.00/40.7300/-73.9600`, `#rent-areas/...`), so links are shareable.

Keyboard: `/` focuses search; with the map focused, arrow keys pan, `+`/`-` zoom and Enter opens
the area under the crosshair; Escape closes the details panel.

## What's on the map

- **Data:** US Census Bureau, American Community Survey 2020–2024 5-year estimates, block-group
  level: B25075 (value of owner-occupied homes, 26 brackets), B25063 (gross rent, 24 brackets +
  no cash rent), B25077/B25064 (medians), B25003 (tenure), B19013 (household income), B25071
  (rent as a share of income), B25001/B25002 (housing units, vacancy). 2024 dollars.
- **Dot colors:** each block group's dots are drawn bracket by bracket from its own distribution
  (with stochastic rounding, so totals are unbiased), so a dot's color is the price bracket of the
  homes it stands for. *Display options → Area medians* colors counties, tracts or block groups
  by their median instead.
- **Dot positions:** random points inside the block group's 2020 census blocks (TIGER/Line
  TABBLOCK20), picking blocks in proportion to their 2020 housing-unit count, so dots fall where
  homes are. Positions are not the locations of real homes.
- **Zoom tiers:** the coarser tiers are random subsets of the finest one (stratified within each
  block group), so zooming in only adds dots; tiers cross-fade at their boundaries.

| tier | 1 dot = | shown at zoom | tiles stored at |
|------|---------|---------------|-----------------|
| t1   | 400 homes | 0–6   | z0–4 |
| t2   | 160 homes | 6–8   | z5 |
| t3   | 64 homes  | 8–10  | z7 |
| t4   | 25 homes  | 10–12 | z9 |
| t5   | 10 homes  | 12+   | z11 |

Value and rent dots are separate archives, so only the mode on screen downloads. Large
archives are split into tile-column shards (under 20 MB each) so every file fits any static
host's per-file limit; `app.js` routes each tile request to its shard.
- **Gaps in the source:** the 2024 ACS release has no rows for 39 block groups in Suffolk
  County, NY (most of Brentwood and Wyandanch). `01_acs.py` gives them the county total minus the
  published block groups, split by 2020 housing units, and flags them as estimates.

## Run locally

PMTiles needs HTTP range requests, which `python3 -m http.server` does not support. Use, e.g.:

```
npx http-server housing -p 8000     # then open http://localhost:8000
```

## Rebuilding the data

Needs Python 3.11+ with `numpy pandas geopandas pyogrio shapely pmtiles`, and
[tippecanoe](https://github.com/felt/tippecanoe) on the PATH (or `TIPPECANOE=/path/to/tippecanoe`).
Downloads (~10 GB, mostly census blocks) go in `pipeline/work/` (or `$HOUSING_WORK`), which is
not published:

```
work/acs/<table>.dat    https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-<table>.dat
                        for b25075 b25063 b25077 b25064 b25003 b25071 b19013 b25001 b25002
work/blocks/<SS>.zip    https://www2.census.gov/geo/tiger/TIGER2024/TABBLOCK20/tl_2024_<SS>_tabblock20.zip  (each state + DC + PR)
work/geo/cb_bg.zip      https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_bg_500k.zip
work/geo/cb_tract.zip   .../cb_2024_us_tract_500k.zip
work/geo/cb_county.zip  .../cb_2024_us_county_500k.zip
work/geo/cb_state.zip   .../cb_2024_us_state_500k.zip
work/geo/cb_nation.zip  .../cb_2024_us_nation_5m.zip
work/geo/cb_place.zip   .../cb_2024_us_place_500k.zip
work/geo/cb_zcta.zip    https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip
```

Then, from `pipeline/`:

```
python3 01_acs.py        # parse ACS tables (20 s)
python3 02_dots.py       # 13 million dots, placed in census blocks (~10 min on 4 cores)
python3 03_tiles.py      # dot tiers -> data/dots (~1 min)
python3 04_areas.py      # outlines, detail files, search index (~8 min)
python3 05_mask.py       # data/mask.json, data/us.json
python3 06_manifest.py   # data/manifest.json
```

Random seeds are fixed, so a rebuild from the same inputs gives the same dots.

## Deploying on the subdomain

GitHub Pages allows one custom domain per repository, so to serve this folder at
`housing.theojaffee.net`:

1. Put the contents of this folder at the root of its own repository (e.g. `tjaffee99/housing`),
   keeping the `CNAME` file, and enable Pages for it (Settings → Pages → deploy from branch).
   `pipeline/` can stay or go; the site doesn't use it.
2. Add a DNS record: `CNAME  housing  →  tjaffee99.github.io`.
3. Once the certificate is issued, tick "Enforce HTTPS".

Any other static host that supports HTTP range requests works too (Cloudflare Pages, Netlify,
Vercel, S3/R2): point it at this folder with no build command. Every data file is under 20 MB.

Licenses: MapLibre GL JS (BSD-3-Clause, `vendor/MAPLIBRE-LICENSE.txt`), PMTiles (BSD-3-Clause).
Census data is public domain. Base map © OpenStreetMap contributors (ODbL), tiles by OpenFreeMap
(OpenMapTiles schema).
