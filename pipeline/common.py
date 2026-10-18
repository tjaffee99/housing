"""Shared constants for the housing dot-map pipeline.

Run the steps in order (see README.md):
    python3 01_acs.py      # ACS 2020-2024 5-year tables  -> work/acs.pkl
    python3 02_dots.py     # one dot per N homes, placed in 2020 census blocks -> work/dots/*.npz
    python3 03_base.py     # uncapped reference prices and market rents per dot -> work/base/
    python3 04_prices.py   # restate at current prices; monthly costs; area statistics -> work/now/
    python3 05_tiles.py    # dots -> nested zoom tiers of vector tiles (PMTiles), one set per mode
    python3 06_areas.py    # county / tract / block-group / ZIP outlines and statistics
    python3 07_search.py   # search index
    python3 08_mask.py     # dim everything outside the US
    python3 09_manifest.py # data/manifest.json
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)                       # housing/
OUT = os.path.join(SITE, 'data')                   # published data
WORK = os.environ.get('HOUSING_WORK', os.path.join(HERE, 'work'))   # downloads + intermediates (not published)

ACS_YEAR = 2024        # ACS 5-year, 2020-2024
HUD_FY = 2027          # HUD Fair Market Rents (fiscal year)
STATES = ['01', '02', '04', '05', '06', '08', '09', '10', '11', '12', '13', '15', '16', '17', '18', '19', '20',
          '21', '22', '23', '24', '25', '26', '27', '28', '29', '30', '31', '32', '33', '34', '35', '36', '37',
          '38', '39', '40', '41', '42', '44', '45', '46', '47', '48', '49', '50', '51', '53', '54', '55', '56', '72']

# ACS B25075 (value of owner-occupied units): 26 bins, lower edges in dollars.
VALUE_EDGES = [0, 10_000, 15_000, 20_000, 25_000, 30_000, 35_000, 40_000, 50_000, 60_000, 70_000, 80_000,
               90_000, 100_000, 125_000, 150_000, 175_000, 200_000, 250_000, 300_000, 400_000, 500_000,
               750_000, 1_000_000, 1_500_000, 2_000_000]
# ACS B25063 (gross rent of renter-occupied units): 24 cash-rent bins + bin 24 = "no cash rent".
RENT_EDGES = [0, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800, 900, 1000,
              1250, 1500, 2000, 2500, 3000, 3500]
NO_CASH_RENT = 24

# Display classes (what the legend and colors use): lower edges in current dollars, last class open.
VALUE_CLASSES = [0, 100_000, 150_000, 200_000, 250_000, 300_000, 400_000, 500_000, 750_000, 1_000_000,
                 1_500_000, 2_000_000, 3_000_000, 5_000_000, 10_000_000]
# rents and monthly costs share one scale, so a color means the same monthly outlay in every mode
MONTHLY_CLASSES = [0, 500, 750, 1000, 1250, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10_000, 20_000]
NO_CASH_CLASS = len(MONTHLY_CLASSES)          # paid-rent mode: renters who pay no cash rent

# Buying today (monthly cost mode): 20% down, 30-year fixed at the latest Freddie Mac rate,
# property tax at the area's effective rate, insurance as a share of value.
DOWN = 0.20
TERM_MONTHS = 360
INSURANCE = 0.005          # per year, of value
CA_MIN_TAX = 0.011         # California resets a buyer's tax base to the price (Proposition 13)

# Dot tiers. Every tier is a random subset of the finest one, so dots never jump
# around as you zoom: zooming in only adds dots. `zooms` are the tile zooms stored
# (one below where a tier starts showing: dot positions are random within their block
# anyway, and coarser coordinates make much smaller tiles); MapLibre over-zooms them.
# Ratios step by about 2.5x per tier so density changes evenly as you zoom.
FINEST = 10
TIERS = [
    dict(name='t1', ratio=400, zooms=[0, 1, 2, 3, 4], show=(0, 6)),
    dict(name='t2', ratio=160, zooms=[5], show=(6, 8)),
    dict(name='t3', ratio=64, zooms=[7], show=(8, 10)),
    dict(name='t4', ratio=25, zooms=[9], show=(10, 12)),
    dict(name='t5', ratio=FINEST, zooms=[11], show=(12, 24)),
]
EXTENT = 4096
WORLD_BITS = 24        # dots are stored as world coords at 2^24 units = zoom 12 x extent 4096
SHARD_BYTES = 45_000_000   # split any archive bigger than this (GitHub's per-file limit is 100 MB)
