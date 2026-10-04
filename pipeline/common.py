"""Shared constants for the housing dot-map pipeline.

Run the steps in order (see README.md):
    python3 01_acs.py      # ACS 2020-2024 5-year tables  -> work/acs.pkl
    python3 02_dots.py     # one dot per N homes, placed in 2020 census blocks -> work/dots/*.npz
    python3 03_tiles.py    # dots -> nested zoom tiers of vector tiles (PMTiles)
    python3 04_areas.py    # county / tract / block-group outlines, stats, search index
    python3 05_mask.py     # dim everything outside the US
    python3 06_manifest.py # data/manifest.json
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)                       # housing/
OUT = os.path.join(SITE, 'data')                   # published data
WORK = os.environ.get('HOUSING_WORK', os.path.join(HERE, 'work'))   # downloads + intermediates (not published)

ACS_YEAR = 2024        # ACS 5-year, 2020-2024
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

# Display classes (what the legend and colors use), as [first ACS bin, last ACS bin] inclusive.
VALUE_CLASSES = [  # label, lo bin, hi bin
    ('Under $100K', 0, 12), ('$100–150K', 13, 14), ('$150–200K', 15, 16), ('$200–250K', 17, 17),
    ('$250–300K', 18, 18), ('$300–400K', 19, 19), ('$400–500K', 20, 20), ('$500–750K', 21, 21),
    ('$750K–1M', 22, 22), ('$1–1.5M', 23, 23), ('$1.5–2M', 24, 24), ('$2M+', 25, 25)]
RENT_CLASSES = [
    ('Under $500', 0, 8), ('$500–750', 9, 13), ('$750–1,000', 14, 16), ('$1,000–1,250', 17, 17),
    ('$1,250–1,500', 18, 18), ('$1,500–2,000', 19, 19), ('$2,000–2,500', 20, 20), ('$2,500–3,000', 21, 21),
    ('$3,000–3,500', 22, 22), ('$3,500+', 23, 23), ('No cash rent', 24, 24)]

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
SHARD_BYTES = 20_000_000   # split any archive bigger than this (static-host per-file limits)
