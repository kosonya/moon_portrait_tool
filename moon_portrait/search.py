"""Candidate (camera, model) pair search.

For each lunar sample (time, az, alt), find pairs of grid cells (C, M) such that:
  1. M is at distance d in [d_min, d_max] from C in azimuth direction az
  2. The elevation angle from camera eye to model head matches alt within tol
  3. Line of sight from C to M head is unobstructed by terrain
  4. Beyond M, in direction (az, alt), the sky is clear for some checking range

Implementation notes
--------------------
The (1)+(2) check is vectorized: for fixed (az, alt, d), shift the entire DEM
by the (dx, dy) corresponding to that direction+distance, and compute the
elevation delta to identify candidate cells. We sweep d in fixed steps.

LOS and sky checks are per-candidate and use a step-wise ray walk through the
bilinear-interpolated DEM.

The output candidates are deduplicated by snapping camera and model positions
to a coarse grid (default 25 m) and time to a coarse bin (default 5 min). The
same (snap_C, snap_M, snap_t) seen with multiple azimuth samples is collapsed
to one record keeping the median alt match.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Sequence

import numpy as np
from pyproj import Transformer

from .astro import LunarSample, LunarWindow
from .dem import TerrainGrid, UTM10N, WGS84

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class SearchConfig:
    # Geometry
    d_min_m: float = 250.0
    d_max_m: float = 500.0
    d_step_m: float = 25.0           # distance sweep step
    alt_tol_deg: float = 0.15        # ±allowed deviation of look-up angle from moon altitude
    az_tol_deg: float = 0.30         # ±allowed deviation of bearing from moon azimuth
                                      # (used when picking the lunar sample for each candidate)
    eye_height_m: float = 1.5
    model_height_m: float = 1.7

    # LOS / sky checks
    los_step_m: float = 10.0         # along-ray sampling step
    los_clearance_m: float = 0.5     # minimum margin terrain must be below the LOS line
    sky_check_range_m: float = 2000.0  # how far beyond M to check sky clearance

    # Sample deduplication: collapse lunar samples whose (az, alt) round to the
    # same bin into a single representative. The Moon moves slowly so 0.3°
    # bins still pick up distinct candidates while cutting sample count.
    sample_az_bin_deg: float = 0.3
    sample_alt_bin_deg: float = 0.1

    # Output deduplication: snap camera & model to grid this size for dedup.
    dedup_xy_snap_m: float = 50.0
    dedup_time_snap_minutes: float = 10.0


@dataclasses.dataclass
class Candidate:
    time_utc: datetime
    az_required_deg: float
    alt_required_deg: float
    az_actual_deg: float
    alt_actual_deg: float
    distance_m: float
    elev_gain_m: float          # E_M - E_C
    camera_x: float             # UTM 10N
    camera_y: float
    camera_elev_m: float
    camera_lat: float
    camera_lon: float
    model_x: float
    model_y: float
    model_elev_m: float
    model_lat: float
    model_lon: float
    moon_phase_deg: float
    sun_alt_deg: float
    # Optional OSM-derived "is in a public-access polygon" annotation.
    # None = not annotated (skipped or failed). Set by public_land.annotate().
    camera_public: bool | None = None
    model_public: bool | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---- search ------------------------------------------------------------------


def search_window(
    grid: TerrainGrid,
    samples: Sequence[LunarSample],
    cfg: SearchConfig,
) -> list[Candidate]:
    """Run vectorized candidate search across all (az, alt) samples of a window.

    All hot paths are numpy. Per (sample, distance d):
      1. Shift DEM, mask cells whose look-up angle to (model_at_d) matches alt.
      2. Accumulate raw matching cells across all distances.
    Then per sample:
      3. Cluster by snap'd (camera_bin, model_bin); keep one rep per group.
      4. Batched LOS + sky check on survivors.
      5. Emit Candidate records.
    """
    elev = grid.elev
    res = grid.res
    snap = cfg.dedup_xy_snap_m
    snap_cells = max(1, int(round(snap / res)))

    out: list[Candidate] = []
    to_wgs84 = Transformer.from_crs(UTM10N, WGS84, always_xy=True)

    for sample in samples:
        az = math.radians(sample.az_deg)
        alt = math.radians(sample.alt_deg)
        tan_alt = math.tan(alt)
        alt_tol_rad = math.radians(cfg.alt_tol_deg)
        ux, uy = math.sin(az), math.cos(az)

        distances = np.arange(cfg.d_min_m, cfg.d_max_m + 0.5 * cfg.d_step_m,
                              cfg.d_step_m, dtype=np.float64)

        all_r, all_c, all_d, all_ec, all_em, all_alpha = [], [], [], [], [], []
        for d in distances:
            dx, dy = ux * d, uy * d
            d_row, d_col = -dy / res, dx / res
            shifted = _shifted_bilinear(elev, d_row, d_col, fill=grid.nodata)
            valid = (elev != grid.nodata) & (shifted != grid.nodata)
            delta_h = (shifted - elev) + cfg.model_height_m - cfg.eye_height_m
            req_lo = d * math.tan(alt - alt_tol_rad)
            req_hi = d * math.tan(alt + alt_tol_rad)
            mask = valid & (delta_h >= req_lo) & (delta_h <= req_hi)
            if not mask.any():
                continue
            rs, cs = np.where(mask)
            ec_arr = elev[rs, cs]
            em_arr = shifted[rs, cs]
            alpha_arr = np.degrees(np.arctan2(
                em_arr + cfg.model_height_m - (ec_arr + cfg.eye_height_m), d))
            all_r.append(rs); all_c.append(cs)
            all_d.append(np.full(rs.shape, d, dtype=np.float64))
            all_ec.append(ec_arr); all_em.append(em_arr)
            all_alpha.append(alpha_arr)

        if not all_r:
            continue
        rs = np.concatenate(all_r)
        cs = np.concatenate(all_c)
        ds = np.concatenate(all_d)
        ecs = np.concatenate(all_ec).astype(np.float64)
        ems = np.concatenate(all_em).astype(np.float64)
        alphas = np.concatenate(all_alpha)

        # Numpy clustering by (cam_bin, mod_bin), keep best alt residual.
        cam_kr = rs // snap_cells
        cam_kc = cs // snap_cells
        mod_r_pix = (rs.astype(np.float64) + (-uy * ds / res))
        mod_c_pix = (cs.astype(np.float64) + (ux * ds / res))
        mod_kr = mod_r_pix.astype(np.int64) // snap_cells
        mod_kc = mod_c_pix.astype(np.int64) // snap_cells
        # Composite key (uint64 won't overflow for 100km-scale grids).
        key = (cam_kr.astype(np.int64) * 100000 + cam_kc.astype(np.int64))
        key = key * 10_000_000_000 + (
            mod_kr.astype(np.int64) * 100000 + mod_kc.astype(np.int64))
        resid = np.abs(alphas - sample.alt_deg)
        # Sort by (key, residual asc) so first occurrence of each unique key
        # is the best representative.
        order = np.lexsort((resid, key))
        sk = key[order]
        _, first_idx = np.unique(sk, return_index=True)
        keep = order[first_idx]

        # Batched LOS + sky.
        cam_xs = grid.x0 + (cs[keep] + 0.5) * res
        cam_ys = grid.y0 - (rs[keep] + 0.5) * res
        mod_xs = cam_xs + ux * ds[keep]
        mod_ys = cam_ys + uy * ds[keep]
        ec_k = ecs[keep]; em_k = ems[keep]
        passes = _batch_los_and_sky(
            grid, cam_xs, cam_ys, ec_k + cfg.eye_height_m,
            mod_xs, mod_ys, em_k + cfg.model_height_m,
            ux, uy, tan_alt,
            los_step=cfg.los_step_m, los_clear=cfg.los_clearance_m,
            sky_range=cfg.sky_check_range_m,
        )
        if not passes.any():
            continue
        idx = np.where(passes)[0]
        cam_xs = cam_xs[idx]; cam_ys = cam_ys[idx]
        mod_xs = mod_xs[idx]; mod_ys = mod_ys[idx]
        ec_k = ec_k[idx]; em_k = em_k[idx]
        ds_k = ds[keep][idx]; alphas_k = alphas[keep][idx]

        # Batch transform.
        cam_lons, cam_lats = to_wgs84.transform(cam_xs, cam_ys)
        mod_lons, mod_lats = to_wgs84.transform(mod_xs, mod_ys)

        for i in range(len(idx)):
            out.append(Candidate(
                time_utc=sample.time_utc,
                az_required_deg=sample.az_deg,
                alt_required_deg=sample.alt_deg,
                az_actual_deg=sample.az_deg,
                alt_actual_deg=float(alphas_k[i]),
                distance_m=float(ds_k[i]),
                elev_gain_m=float(em_k[i] - ec_k[i]),
                camera_x=float(cam_xs[i]), camera_y=float(cam_ys[i]),
                camera_elev_m=float(ec_k[i]),
                camera_lat=float(cam_lats[i]), camera_lon=float(cam_lons[i]),
                model_x=float(mod_xs[i]), model_y=float(mod_ys[i]),
                model_elev_m=float(em_k[i]),
                model_lat=float(mod_lats[i]), model_lon=float(mod_lons[i]),
                moon_phase_deg=sample.moon_phase_deg,
                sun_alt_deg=sample.sun_alt_deg,
            ))
    return out


class SearchCancelled(Exception):
    """Raised when an external cancellation event is observed mid-search."""


def iter_search_windows(
    grid: TerrainGrid,
    windows: Sequence[LunarWindow],
    cfg: SearchConfig,
):
    """Generator: yield (window_idx, window, candidates_for_this_window).

    Callers can cancel cooperatively by simply breaking out of the loop;
    whatever they've accumulated so far is a valid partial result that
    can still be dedup'd, water-filtered, and written.
    """
    for i, w in enumerate(windows):
        reps = _dedupe_samples(w.samples, cfg.sample_az_bin_deg,
                               cfg.sample_alt_bin_deg)
        logger.info("Window %d/%d: %s..%s (%d->%d samples)",
                    i + 1, len(windows), w.start_utc, w.end_utc,
                    len(w.samples), len(reps))
        yield i, w, search_window(grid, reps, cfg)


def search_windows(
    grid: TerrainGrid,
    windows: Sequence[LunarWindow],
    cfg: SearchConfig,
) -> list[Candidate]:
    out = []
    for _, _, cands in iter_search_windows(grid, windows, cfg):
        out.extend(cands)
    return out


def _dedupe_samples(samples: Sequence[LunarSample],
                    az_bin: float, alt_bin: float) -> list[LunarSample]:
    """Collapse samples sharing the same (az, alt) bin to one rep (median time)."""
    if not samples:
        return []
    groups: dict[tuple[int, int], list[LunarSample]] = defaultdict(list)
    for s in samples:
        key = (int(round(s.az_deg / az_bin)), int(round(s.alt_deg / alt_bin)))
        groups[key].append(s)
    reps = []
    for g in groups.values():
        g.sort(key=lambda s: s.time_utc)
        reps.append(g[len(g) // 2])
    reps.sort(key=lambda s: s.time_utc)
    return reps


def deduplicate(cands: Iterable[Candidate], cfg: SearchConfig) -> list[Candidate]:
    """Snap camera, model, and time, then keep one per group (best alt match)."""
    snap_xy = cfg.dedup_xy_snap_m
    snap_t = cfg.dedup_time_snap_minutes * 60
    groups: dict[tuple, list[Candidate]] = defaultdict(list)
    for c in cands:
        key = (
            round(c.camera_x / snap_xy), round(c.camera_y / snap_xy),
            round(c.model_x / snap_xy), round(c.model_y / snap_xy),
            round(c.time_utc.timestamp() / snap_t),
        )
        groups[key].append(c)
    out = []
    for g in groups.values():
        # Best alt match (smallest |alt_actual - alt_required|)
        best = min(g, key=lambda c: abs(c.alt_actual_deg - c.alt_required_deg))
        out.append(best)
    return out


# ---- helpers -----------------------------------------------------------------


def _shifted_bilinear(arr: np.ndarray, d_row: float, d_col: float,
                      fill: float) -> np.ndarray:
    """Bilinearly resample arr at position (r + d_row, c + d_col) for every (r, c).

    Out-of-bounds cells are filled with `fill`.
    """
    H, W = arr.shape
    # Integer parts and fractional parts
    r0 = int(math.floor(d_row))
    c0 = int(math.floor(d_col))
    fr = d_row - r0
    fc = d_col - c0
    # Slicing offsets for the 4 sample corners
    out = np.full_like(arr, fill)

    def get_shifted(dr_int: int, dc_int: int) -> np.ndarray:
        # Returns an array same shape as arr with values arr[r + dr_int, c + dc_int]
        # and `fill` where out of bounds.
        shifted = np.full_like(arr, fill)
        src_r_lo = max(0, -dr_int)
        src_r_hi = min(H, H - dr_int)
        src_c_lo = max(0, -dc_int)
        src_c_hi = min(W, W - dc_int)
        if src_r_lo >= src_r_hi or src_c_lo >= src_c_hi:
            return shifted
        dst_r_lo = src_r_lo + dr_int
        dst_r_hi = src_r_hi + dr_int
        dst_c_lo = src_c_lo + dc_int
        dst_c_hi = src_c_hi + dc_int
        shifted[src_r_lo:src_r_hi, src_c_lo:src_c_hi] = \
            arr[dst_r_lo:dst_r_hi, dst_c_lo:dst_c_hi]
        return shifted

    e00 = get_shifted(r0, c0)
    e01 = get_shifted(r0, c0 + 1)
    e10 = get_shifted(r0 + 1, c0)
    e11 = get_shifted(r0 + 1, c0 + 1)

    top = e00 * (1 - fc) + e01 * fc
    bot = e10 * (1 - fc) + e11 * fc
    out = top * (1 - fr) + bot * fr
    # If any corner was fill, mark out as fill.
    bad = (e00 == fill) | (e01 == fill) | (e10 == fill) | (e11 == fill)
    out[bad] = fill
    return out


def _batch_los_and_sky(
    grid: TerrainGrid,
    x0: np.ndarray, y0: np.ndarray, h0: np.ndarray,
    x1: np.ndarray, y1: np.ndarray, h1: np.ndarray,
    ux: float, uy: float, tan_alt: float,
    los_step: float, los_clear: float,
    sky_range: float,
) -> np.ndarray:
    """Vectorized LOS (camera→model) and sky (beyond model) checks.

    For LOS: each candidate's ray is sampled at fixed parametric points t in
    (0, 1) at step los_step / d. For sky: sampled at fixed distances out to
    sky_range, in the same (ux, uy) direction.

    Returns a 1-D bool array of length len(x0): True iff candidate passes both.
    """
    n = len(x0)
    if n == 0:
        return np.zeros(0, dtype=bool)

    # LOS: use a uniform number of interior steps. The longest ray dominates.
    d = np.hypot(x1 - x0, y1 - y0)
    n_los = max(2, int(np.ceil(d.max() / los_step)))
    # interior parametric ts, excluding endpoints
    ts = np.linspace(0.0, 1.0, n_los + 1)[1:-1]   # shape (n_los-1,)
    # broadcast: pts of shape (n, n_los-1)
    xs = x0[:, None] + (x1 - x0)[:, None] * ts[None, :]
    ys = y0[:, None] + (y1 - y0)[:, None] * ts[None, :]
    line_h = h0[:, None] + (h1 - h0)[:, None] * ts[None, :]
    terrain = grid.sample(xs.ravel(), ys.ravel()).reshape(xs.shape)
    bad_los = (terrain == grid.nodata) | (terrain > line_h - los_clear)
    los_pass = ~bad_los.any(axis=1)

    # Sky: fixed distances beyond model, common to all candidates
    n_sky = max(1, int(sky_range / los_step))
    ds_sky = np.arange(1, n_sky + 1) * los_step  # shape (n_sky,)
    xs_s = x1[:, None] + ux * ds_sky[None, :]
    ys_s = y1[:, None] + uy * ds_sky[None, :]
    line_h_s = h1[:, None] + ds_sky[None, :] * tan_alt
    terrain_s = grid.sample(xs_s.ravel(), ys_s.ravel()).reshape(xs_s.shape)
    # nodata in the sky region = off-grid, treat as clear
    bad_sky = (terrain_s != grid.nodata) & (terrain_s > line_h_s - los_clear)
    sky_pass = ~bad_sky.any(axis=1)

    return los_pass & sky_pass


def _line_of_sight_clear(grid: TerrainGrid,
                         x0: float, y0: float, h0: float,
                         x1: float, y1: float, h1: float,
                         step: float, clearance: float) -> bool:
    """Return True iff terrain along [P0, P1] stays at least `clearance` below
    the straight line from P0 to P1.

    Heights h0, h1 are absolute (eye + terrain, head + terrain).
    """
    dx, dy = x1 - x0, y1 - y0
    d = math.hypot(dx, dy)
    if d <= step:
        return True
    n = max(1, int(d / step))
    # Skip the endpoints (we already know they're fine).
    t = np.linspace(0.0, 1.0, n + 1)[1:-1]
    xs = x0 + dx * t
    ys = y0 + dy * t
    line_h = h0 + (h1 - h0) * t
    terrain = grid.sample(xs, ys)
    # Treat nodata as blocking.
    if (terrain == grid.nodata).any():
        return False
    return bool(np.all(terrain <= line_h - clearance))


def _sky_clear(grid: TerrainGrid,
               x: float, y: float, h: float,
               ux: float, uy: float, tan_alt: float,
               range_m: float, step: float, clearance: float) -> bool:
    """Return True iff terrain beyond M, in direction (ux, uy, tan_alt), stays
    below the line rising from M at angle alt.

    We sample at distances [step, 2*step, ..., range_m] beyond M and require
    terrain there to be below h + d * tan_alt - clearance.
    """
    n = max(1, int(range_m / step))
    ds = np.arange(1, n + 1) * step
    xs = x + ux * ds
    ys = y + uy * ds
    line_h = h + ds * tan_alt
    terrain = grid.sample(xs, ys)
    valid = terrain != grid.nodata
    # If we run off the grid, consider that side clear (we'll filter heavily
    # near edges later if needed).
    if not valid.any():
        return True
    return bool(np.all(terrain[valid] <= line_h[valid] - clearance))
