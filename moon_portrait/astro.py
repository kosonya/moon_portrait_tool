"""Lunar window finder.

For a date range and observing location, compute the (start, end) time windows
where the Moon is near full and observable for a Moon-portrait shot:
  - Moon altitude within [alt_min, alt_max]
  - Sun altitude <= sun_alt_max (e.g., -10° for astronomical twilight or darker)
  - Moon phase within phase_tolerance_deg of full (180°)

Then, samples each window at a fixed time step and emits (time, az, alt)
triples that the terrain search consumes.

Geocentric quantities (full-moon times) are independent of observer; topocentric
quantities (alt/az and sun depression) use the provided lat/lon. The Bay Area
is small enough (~100 km) that using a single reference observer for window-
finding introduces negligible error in window edges; per-candidate alt/az is
recomputed from the actual camera location during the final geometry check.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np
from skyfield import almanac
from skyfield.api import Loader, Star, wgs84


# JPL DE421 ephemeris (covers 1900-2050, 17 MB). Good enough for our window.
EPHEMERIS_FILE = "de421.bsp"


@dataclasses.dataclass(frozen=True)
class LunarSample:
    """One (time, az, alt) sample from a lunar trajectory window."""
    time_utc: datetime
    az_deg: float           # 0 = north, 90 = east
    alt_deg: float          # 0 = horizon, 90 = zenith
    moon_phase_deg: float   # 0 = new, 180 = full
    sun_alt_deg: float      # negative below horizon

    def __repr__(self) -> str:
        return (f"LunarSample(t={self.time_utc:%Y-%m-%d %H:%M} UTC, "
                f"az={self.az_deg:.2f}°, alt={self.alt_deg:.2f}°, "
                f"phase={self.moon_phase_deg:.1f}°, sun={self.sun_alt_deg:.1f}°)")


@dataclasses.dataclass(frozen=True)
class LunarWindow:
    """A continuous time interval matching all astronomical constraints."""
    start_utc: datetime
    end_utc: datetime
    samples: list[LunarSample]

    @property
    def duration_minutes(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 60.0


class AstroEngine:
    def __init__(
        self,
        observer_lat_deg: float,
        observer_lon_deg: float,
        observer_elev_m: float = 0.0,
        ephem_dir: str | Path = "data",
    ):
        self.observer_lat = observer_lat_deg
        self.observer_lon = observer_lon_deg
        self.observer_elev = observer_elev_m
        ephem_dir = Path(ephem_dir)
        ephem_dir.mkdir(parents=True, exist_ok=True)
        loader = Loader(str(ephem_dir))
        self.ts = loader.timescale()
        self.eph = loader(EPHEMERIS_FILE)
        self.sun = self.eph["sun"]
        self.earth = self.eph["earth"]
        self.moon = self.eph["moon"]
        self.observer = self.earth + wgs84.latlon(
            observer_lat_deg, observer_lon_deg, observer_elev_m
        )

    # ---- core astronomy helpers ----------------------------------------------

    def full_moons(self, t0: datetime, t1: datetime) -> list[datetime]:
        """Return UTC datetimes of full moons strictly within [t0, t1]."""
        t_start = self.ts.from_datetime(_aware_utc(t0))
        t_end = self.ts.from_datetime(_aware_utc(t1))
        times, phases = almanac.find_discrete(
            t_start, t_end, almanac.moon_phases(self.eph)
        )
        # phase code 2 == Full Moon (0=new, 1=first quarter, 2=full, 3=last quarter)
        return [t.utc_datetime() for t, p in zip(times, phases) if int(p) == 2]

    def alt_az(self, t) -> tuple[float, float]:
        """Topocentric (alt_deg, az_deg) of the Moon at Skyfield time t."""
        astrometric = self.observer.at(t).observe(self.moon).apparent()
        alt, az, _ = astrometric.altaz()
        return float(alt.degrees), float(az.degrees)

    def sun_alt(self, t) -> float:
        """Topocentric altitude of the Sun in degrees."""
        astrometric = self.observer.at(t).observe(self.sun).apparent()
        alt, _, _ = astrometric.altaz()
        return float(alt.degrees)

    def moon_phase_deg(self, t) -> float:
        """Geocentric Moon phase angle (0 = new, 180 = full)."""
        return float(almanac.moon_phase(self.eph, t).degrees)

    # ---- window finder -------------------------------------------------------

    def lunar_windows(
        self,
        t0: datetime,
        t1: datetime,
        alt_min_deg: float = 3.0,
        alt_max_deg: float = 20.0,
        sun_alt_max_deg: float = -10.0,
        sun_alt_min_deg: float = -90.0,
        phase_tolerance_deg: float = 15.0,
        coarse_step_minutes: float = 5.0,
        sample_step_minutes: float = 2.0,
    ) -> list[LunarWindow]:
        """Find lunar windows matching all constraints between t0 and t1.

        Sun-altitude band: sample passes iff sun_alt_min ≤ sun_alt ≤ sun_alt_max.
        Defaults give the original nighttime behavior (any sun altitude
        from horizon-pole down through −90° works, as long as the sun is also
        below sun_alt_max = −10° = astronomical twilight). Set
        sun_alt_min_deg=0 (and sun_alt_max_deg=90) for daytime-only searches.

        Algorithm:
          1. Find every full moon in [t0 - tol, t1 + tol] where tol corresponds
             to phase_tolerance_deg (Moon moves ~12°/day so 15° ≈ 30 hr).
          2. For each full moon, scan a ±N-day window at coarse_step_minutes
             granularity; flag samples passing all constraints.
          3. Merge consecutive passing samples into windows; resample each
             window at sample_step_minutes for the final trajectory.
        """
        t0 = _aware_utc(t0)
        t1 = _aware_utc(t1)
        # Moon moves ~12.2°/day in phase; widen the search by enough time to
        # cover phase_tolerance_deg on either side, then a 1-day safety margin.
        tol_days = phase_tolerance_deg / 12.2 + 1.0
        full_moons = self.full_moons(
            t0 - timedelta(days=tol_days),
            t1 + timedelta(days=tol_days),
        )

        windows: list[LunarWindow] = []
        for fm in full_moons:
            scan_start = max(t0, fm - timedelta(days=tol_days))
            scan_end = min(t1, fm + timedelta(days=tol_days))
            if scan_end <= scan_start:
                continue
            windows.extend(self._scan_around_full_moon(
                scan_start, scan_end,
                alt_min_deg, alt_max_deg,
                sun_alt_max_deg, sun_alt_min_deg,
                phase_tolerance_deg,
                coarse_step_minutes, sample_step_minutes,
            ))
        return windows

    def _scan_around_full_moon(
        self,
        scan_start: datetime,
        scan_end: datetime,
        alt_min: float, alt_max: float,
        sun_alt_max: float, sun_alt_min: float,
        phase_tol: float,
        coarse_step_min: float, sample_step_min: float,
    ) -> list[LunarWindow]:
        # Coarse pass: vectorized over a numpy array of times.
        n = int(np.ceil((scan_end - scan_start).total_seconds() / 60.0
                        / coarse_step_min)) + 1
        offsets_min = np.arange(n) * coarse_step_min
        times_utc = [scan_start + timedelta(minutes=float(m))
                     for m in offsets_min]
        ts_array = self.ts.from_datetimes(times_utc)

        # Vectorize alt/az + sun + phase. Skyfield supports Time arrays.
        moon_app = self.observer.at(ts_array).observe(self.moon).apparent()
        moon_alt, moon_az, _ = moon_app.altaz()
        moon_alt = np.asarray(moon_alt.degrees)
        moon_az = np.asarray(moon_az.degrees)

        sun_app = self.observer.at(ts_array).observe(self.sun).apparent()
        sun_alt, _, _ = sun_app.altaz()
        sun_alt = np.asarray(sun_alt.degrees)

        phase = np.asarray(almanac.moon_phase(self.eph, ts_array).degrees)

        passing = (
            (moon_alt >= alt_min) &
            (moon_alt <= alt_max) &
            (sun_alt <= sun_alt_max) &
            (sun_alt >= sun_alt_min) &
            (np.abs(phase - 180.0) <= phase_tol)
        )

        windows: list[LunarWindow] = []
        for start_i, end_i in _runs_of_true(passing):
            w_start = times_utc[start_i]
            w_end = times_utc[end_i]
            samples = self._sample_window(
                w_start, w_end, sample_step_min,
            )
            if samples:
                windows.append(LunarWindow(w_start, w_end, samples))
        return windows

    def _sample_window(
        self, start: datetime, end: datetime, step_min: float,
    ) -> list[LunarSample]:
        if end <= start:
            return []
        n = int(np.ceil((end - start).total_seconds() / 60.0 / step_min)) + 1
        offsets = np.arange(n) * step_min
        times_utc = [start + timedelta(minutes=float(m)) for m in offsets]
        ts_array = self.ts.from_datetimes(times_utc)

        moon_app = self.observer.at(ts_array).observe(self.moon).apparent()
        moon_alt, moon_az, _ = moon_app.altaz()
        moon_alt = np.asarray(moon_alt.degrees)
        moon_az = np.asarray(moon_az.degrees)
        sun_app = self.observer.at(ts_array).observe(self.sun).apparent()
        sun_alt, _, _ = sun_app.altaz()
        sun_alt = np.asarray(sun_alt.degrees)
        phase = np.asarray(almanac.moon_phase(self.eph, ts_array).degrees)

        return [
            LunarSample(
                time_utc=t,
                az_deg=float(moon_az[i]),
                alt_deg=float(moon_alt[i]),
                moon_phase_deg=float(phase[i]),
                sun_alt_deg=float(sun_alt[i]),
            )
            for i, t in enumerate(times_utc)
        ]


# ---- helpers -----------------------------------------------------------------


def _aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _runs_of_true(arr: np.ndarray) -> list[tuple[int, int]]:
    """Return list of (start, end_inclusive) index pairs for runs of True."""
    if arr.size == 0:
        return []
    diffs = np.diff(arr.astype(np.int8))
    starts = list(np.where(diffs == 1)[0] + 1)
    ends = list(np.where(diffs == -1)[0])
    if arr[0]:
        starts.insert(0, 0)
    if arr[-1]:
        ends.append(arr.size - 1)
    return list(zip(starts, ends))
