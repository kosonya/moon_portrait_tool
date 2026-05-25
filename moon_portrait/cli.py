"""Command-line entry point for moon-portrait finder.

Example:
  python -m moon_portrait.cli \
      --bbox -122.5,37.2,-121.5,38.0 \
      --start 2026-05-24 --end 2026-09-01 \
      --dem-res 30 \
      --out-dir results/east_bay_summer
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from .astro import AstroEngine
from .dem import load_terrain
from .output import write_csv, write_geojson, write_map
from .search import SearchConfig, deduplicate, search_windows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", required=True,
                    help="west,south,east,north in WGS84 degrees")
    ap.add_argument("--start", required=True, help="UTC date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="UTC date YYYY-MM-DD")
    ap.add_argument("--observer-lat", type=float, default=37.39,
                    help="reference observer latitude (Mountain View default)")
    ap.add_argument("--observer-lon", type=float, default=-122.08)
    ap.add_argument("--dem-res", type=float, default=30.0,
                    help="DEM resampling resolution in meters")
    ap.add_argument("--d-min", type=float, default=250.0)
    ap.add_argument("--d-max", type=float, default=500.0)
    ap.add_argument("--alt-min", type=float, default=3.0)
    ap.add_argument("--alt-max", type=float, default=20.0)
    ap.add_argument("--sun-alt-max", type=float, default=-10.0)
    ap.add_argument("--phase-tol", type=float, default=15.0,
                    help="±phase tolerance from full moon (degrees)")
    ap.add_argument("--alt-tol", type=float, default=0.15,
                    help="tolerance on look-up angle vs moon altitude (deg)")
    ap.add_argument("--sample-step-min", type=float, default=10.0)
    ap.add_argument("--snap-m", type=float, default=75.0,
                    help="output dedup grid size (meters)")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("moon_portrait")

    bbox = tuple(float(x) for x in args.bbox.split(","))
    assert len(bbox) == 4, "bbox needs 4 numbers"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(args.data_dir)

    log.info("Loading DEM for bbox %s at %sm resolution...", bbox, args.dem_res)
    grid = load_terrain(bbox, args.dem_res,
                        cache_dir=data_dir / "dem_cache")
    log.info("  loaded %sx%s cells (%.1f km x %.1f km)",
             grid.elev.shape[0], grid.elev.shape[1],
             grid.elev.shape[1] * grid.res / 1000,
             grid.elev.shape[0] * grid.res / 1000)

    eng = AstroEngine(args.observer_lat, args.observer_lon,
                      ephem_dir=data_dir)
    t0 = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    t1 = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    log.info("Finding lunar windows %s..%s", t0.date(), t1.date())
    windows = eng.lunar_windows(
        t0, t1,
        alt_min_deg=args.alt_min, alt_max_deg=args.alt_max,
        sun_alt_max_deg=args.sun_alt_max,
        phase_tolerance_deg=args.phase_tol,
        sample_step_minutes=args.sample_step_min,
    )
    log.info("  %d windows, %d samples",
             len(windows), sum(len(w.samples) for w in windows))

    cfg = SearchConfig(
        d_min_m=args.d_min, d_max_m=args.d_max,
        alt_tol_deg=args.alt_tol,
        dedup_xy_snap_m=args.snap_m,
    )
    log.info("Searching terrain...")
    raw = search_windows(grid, windows, cfg)
    log.info("  %d raw candidates", len(raw))
    dedup = deduplicate(raw, cfg)
    log.info("  %d after dedup", len(dedup))

    # Sort: prefer larger distance, then better alt match
    dedup.sort(key=lambda c: (-c.distance_m,
                              abs(c.alt_actual_deg - c.alt_required_deg)))

    write_csv(dedup, out_dir / "candidates.csv")
    write_geojson(dedup, out_dir / "candidates.geojson")
    write_map(dedup, out_dir / "map.html",
              center_lat=args.observer_lat, center_lon=args.observer_lon)
    log.info("Wrote results to %s", out_dir)


if __name__ == "__main__":
    main()
