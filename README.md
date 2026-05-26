# Moon-portrait location finder

Finds (camera, model) terrain pairs in the SF Bay Area where, on a specific
date and time, the full Moon will align with a model standing at the target
elevation angle, satisfying all of:

- distance from camera to model in [d_min, d_max] (default 250 – 500 m, so the
  1.7 m model subtends 0.2°–0.4°, comparable to the Moon's 0.5° disk)
- elevation angle from the camera's eye to the model's head in [3°, 20°]
- straight-line bearing from camera to model exactly aligns with the Moon's
  azimuth at the chosen moment
- the Moon is within ±15° of full
- the Sun is at least 10° below the horizon (astronomical twilight or darker)
- line of sight from camera to model is unobstructed by intervening terrain
- the sky immediately behind the model in the Moon's direction is unobstructed

The tool downloads USGS 3DEP 1/3 arc-second (~10 m) DEM tiles lazily from
AWS via HTTP range reads (no full-tile download for small queries), reprojects
into UTM 10N for metric math, and runs a vectorised numpy search over the
DEM for every Moon trajectory window in the chosen date range.

## What you get

For each search:

- **`map.html`** — interactive Leaflet map (OSM, satellite, topo base layers)
  with one marker pair per unique (camera, model) location. Popups list every
  date/time the alignment recurs, plus Google Maps / Google Earth quick links.
- **`candidates.csv`** — one row per (camera, model, time) triple, all fields.
- **`candidates.geojson`** — same data as a FeatureCollection for QGIS or
  Google Earth Pro import.

## Install

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

GDAL is pulled in transitively via rasterio's wheel — no separate install
needed on macOS / Linux / Windows.

## CLI

```sh
python -m moon_portrait.cli \
    --bbox=-122.5,37.2,-121.5,38.0 \
    --start=2026-05-24 --end=2027-05-24 \
    --observer-lat=37.39 --observer-lon=-122.08 \
    --dem-res=30 \
    --out-dir=results/full_year_bay
```

Run `python -m moon_portrait.cli --help` for every knob.

Tip on bboxes: the TNM Access API returns each USGS 3DEP 1°×1° tile that
intersects the bbox. The Bay Area within 100 km of Mountain View spans
4 tiles (`n38w122`, `n38w123`, `n37w122`, `n37w123`) so the first-time
download is ~30 MB of reprojected GeoTIFF cached locally.

## Web UI

```sh
python -m moon_portrait.webui
# open http://127.0.0.1:5000/
```

Sliders for every constraint; submit re-runs the search and renders the map
inline. Each run's outputs are saved under `results/<run_id>/`.

## Docker / NAS deployment

A `Dockerfile` and `docker-compose.yml` are included so the server can run
24/7 on a NAS (tested target: Asustor / Synology with Portainer), while the
`results/` folder syncs to Google Drive — your laptop sees the same runs.

### What gets containerized

- The container always serves on port `5000` and writes to:
  - `/app/results` — bind-mounted to a host folder you Google-Drive-sync
  - `/app/data`    — Docker named volume (`moon_portrait_cache`) for DEM
    tiles, OSM Overpass responses, and the JPL ephemeris; persists across
    restarts but has no reason to sync.

### Quick start — Docker Compose on the NAS

```sh
ssh into-the-nas
cd /volume1/Mainshare/Insync/.../moon_portrait_tool
cp .env.example .env       # fill in RESULTS_PATH, MOON_PORT, TZ
docker compose up -d --build
```

`http://<nas-ip>:5000/` is now live; `docker compose logs -f` tails it.

### Portainer Stack workflow

1. SSH once to build the image: `docker build -t moon-portrait
   /volume1/Mainshare/Insync/.../moon_portrait_tool`.
2. In Portainer, **Stacks → Add stack**, name it `moon-portrait`.
3. **Build method: Web editor** — paste the contents of `docker-compose.yml`.
4. **Environment variables** — fill in at least `RESULTS_PATH`. Examples:
   - `RESULTS_PATH=/volume1/Mainshare/Insync/sophia.m.kovaleva@gmail.com/Google Drive/proging/moon_photo/Moon portrait/moon_portrait_tool/results`
   - `TZ=America/Los_Angeles`
   - Host port is hardcoded to `5000` in the compose file. To use a
     different one, edit the `ports:` mapping directly (Portainer's stack
     editor doesn't reliably substitute `${VAR:-default}` defaults).
5. **Deploy the stack**. After a few seconds it's up; Portainer's Container
   view lets you start/stop/view logs/exec into it.

When the source changes (you sync a new version via Drive), rebuild from
SSH (`docker compose build` or `docker build -t moon-portrait …`) and
Portainer's **Recreate** button on the container will pull the new image.

### Browsing from the laptop without Docker

The shared `results/` folder is the source of truth — every completed run
is just files. To browse from your laptop while disconnected from the NAS,
spin up a second copy of the web UI pointed at the synced folder:

```sh
cd ~/Insync/sophia.m.kovaleva@gmail.com/Google\ Drive/proging/moon_photo/Moon\ portrait/moon_portrait_tool
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m moon_portrait.webui  # opens at http://127.0.0.1:5000/
```

This local instance has its own DEM/Overpass cache (under `./data/`) but
reads/writes the same `./results/` folder as the NAS instance via Insync.
Running both at the same time is safe as long as only one is actively
running searches — completed-run files don't conflict.

### Notes

- The Flask dev server (used inside the container) is fine for personal
  LAN/VPN use. Don't expose port 5000 to the public Internet — no auth.
- Paths with spaces in them (your `Moon portrait/` folder) work as bind
  mounts as long as `RESULTS_PATH` in `.env` is unquoted but each part of
  the path follows the literal name.
- If your NAS has `docker compose` rather than the older `docker-compose`,
  use the former. The compose file is v3.x and works with either.
- The compose uses `network_mode: host` — the container shares the NAS's
  network stack, binds straight to port 5000 with no NAT. Portainer
  intentionally shows "no IP / no published ports" for host-mode
  containers (that's not a bug — there literally aren't any to display,
  the network namespace is the host's). The service is still reachable at
  `http://<nas-ip>:5000/`.

### Troubleshooting

Container appears in `docker ps` with `Up 1 second` shortly after every
poll, Portainer shows "no IP / no ports", browser gets
`ERR_ADDRESS_UNREACHABLE` → the container is *crashing and restarting in a
loop*. Get the actual reason:

```sh
docker logs --tail 100 moon-portrait
docker inspect moon-portrait --format \
  'status={{.State.Status}} restarts={{.RestartCount}} exit={{.State.ExitCode}} error={{.State.Error}}'
```

Common causes I've hit on NAS appliances:

- **`PermissionError: [Errno 13]` writing to `/app/results`** — the
  bind-mounted Google Drive folder is owned by your NAS user, not by
  `root` (the container's default user). Fix on the NAS:
  ```sh
  chmod -R 0775 "$RESULTS_PATH"
  # or, if it's owned by your user (uid 1000 commonly) and you can't change ownership:
  # edit the Dockerfile to add: USER 1000:1000  before the CMD
  ```
- **`RESULTS_PATH` directory doesn't exist** — bind mounts (unlike named
  volumes) refuse to create missing source paths. `mkdir -p` it first.
- **Asustor / Synology NDP shipping a hardened sysctl** — once in a while
  a kernel-level restriction blocks Python's socket bind. `docker logs`
  will show a `PermissionError` at startup. Usually resolved by running
  the container as host network mode (already configured above).

Once the container is staying up (Status: `Up X seconds (healthy)` or
`(health: starting)` that progresses to `healthy`), test reachability in
order:

```sh
# from the NAS itself
curl -fsS http://127.0.0.1:5000/status
# from another machine on the LAN
curl -fsS http://<nas-ip>:5000/status
```

If the first works but the second doesn't, it's the Asustor firewall (ADM
→ Settings → Network → Network Defender / Firewall — add an inbound rule
for TCP 5000 from your LAN subnet).

## Performance & tuning

The search cost grows roughly with `cells(DEM) × samples(astronomy)`. Rough
guidance, on a 2024 laptop:

| Area              | DEM res | DEM cells | Months | Time     |
|-------------------|---------|-----------|--------|----------|
| 10 × 10 km        | 30 m    | 200 k     | 12     | < 1 min  |
| 10 × 10 km        | 10 m    | 1.8 M     | 12     | ~10 min  |
| 100 × 100 km      | 30 m    | 11 M      | 12     | ~1 hour  |
| 100 × 100 km      | 10 m    | 100 M     | 12     | overnight |

The biggest knobs are:

- `--dem-res` — 30 m is fine for the geometry at 250-500 m distance; drop to
  10 m only to validate a shortlisted candidate.
- `--sample-step-min` — 2 min catches more moments per window, 10 min is
  fine for surveying.
- `--snap-m` — output dedup grid. 75 m is a good default; if you want every
  fine-grained location to count distinctly, drop to 25 m.
- `--alt-tol` — the elevation-angle tolerance. 0.15° (≈ ⅓ of Moon's apparent
  diameter) gives a small set of clean candidates; widen to 0.3° to surface
  near-misses you might adjust your standing point by a few meters to fix.

## Module layout

- `moon_portrait/astro.py` — Skyfield-based lunar window finder
- `moon_portrait/dem.py`   — USGS 3DEP fetch + UTM 10N reprojection + cache
- `moon_portrait/search.py` — vectorised candidate search + LOS / sky checks
- `moon_portrait/output.py` — Folium map, CSV, GeoJSON
- `moon_portrait/cli.py`   — command-line entry point
- `moon_portrait/webui.py` — Flask localhost UI

## Algorithm sketch

For each lunar window (continuous interval where Moon is in [alt_min, alt_max],
Sun ≤ sun_alt_max, Moon phase within tolerance of 180°):

1. Sample the Moon's trajectory at `sample_step_min` intervals — gives
   `(time, az, alt)` samples.
2. For each sample, sweep candidate camera-to-model distances `d` in
   `[d_min, d_max]`. Shift the DEM by the displacement vector
   `(d·sin(az), d·cos(az))` and compute, for every DEM cell taken as the
   camera, the elevation delta to its model neighbour. Mask cells whose
   look-up angle to `(model_elev + 1.7 m)` matches the Moon's altitude
   within `alt_tol`.
3. Cluster matching cells onto the output dedup grid (by camera *and* model
   position) so contiguous valid patches collapse to one representative.
4. Batched line-of-sight walk (camera eye → model head) and sky-clearance
   walk (model head outward in the Moon direction) rejects candidates
   blocked by intervening terrain.
5. Across all windows, candidates sharing a (camera, model, time) bin
   are deduplicated; output groups by spatial pair so each map marker is a
   reusable physical setup, with the popup listing every recurring opportunity.

## Things this version does *not* do

- **OSM-based filtering of accessibility / openness.** Surfaced candidates
  may sit on private land, in dense forest, or in a city block. The user is
  expected to verify each shortlisted candidate in Google Maps / Earth
  before driving out. (A future pass could use OSM `leisure=park`,
  `boundary=protected_area`, and NLCD canopy density to flag likely-open
  candidates — see TODO in `output.py`.)
- **Vegetation height.** USGS 3DEP is bare-earth; a 30 m oak right on the
  model's silhouette will not be flagged. Treat dense-canopy areas
  cautiously.
- **Atmospheric refraction.** At Moon altitudes ≥ 3°, refraction shifts the
  apparent altitude by 0.1°-0.2°. Within our default `alt_tol` (0.15°) but
  worth being aware of when validating.
- **Local horizon obstruction from beyond the DEM.** If your bbox edge
  cuts off a mountain that would block the Moon, the sky-clearance check
  treats that as "clear" rather than failing.

## Validation note

Tested 2026-05 to 2026-08 over Mission Peak / Sunol Wilderness (16 × 11 km):
44,930 raw candidates condensed to ~23,000 spatially-unique (camera, model)
pairs. Top pairs all hit max distance 500 m with elevation gains matching
`atan(gain/d) = alt` within ±0.03°. Geometry independently verified by hand.
