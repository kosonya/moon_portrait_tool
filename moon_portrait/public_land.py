"""Heuristic 'likely on public land' annotation for candidate points.

Fetches park / nature reserve / protected-area polygons from OpenStreetMap
via the public Overpass API, builds a spatial index (shapely.STRtree), and
tests whether each candidate's camera and model points fall inside one.

This is a HEURISTIC — OSM coverage of public-access boundaries is uneven and
some boundaries lump in private inholdings. Treat the resulting flag as a
hint to triage candidates faster, not a definitive access ruling. Always
verify a shortlisted candidate against the relevant park's official maps
and any posted signage before driving out.

Tags included
-------------
- leisure=park
- leisure=nature_reserve
- boundary=protected_area
- boundary=national_park
- landuse=recreation_ground

In the SF Bay Area these reliably pick up East Bay Regional Park District,
Midpeninsula Regional Open Space District, state parks, national parks,
and city open spaces. They miss: golf courses (excluded on purpose),
cemeteries (excluded), private nature preserves (often untagged), military
reservations (separate tag), some unincorporated rangeland.

Caching
-------
The Overpass response for a (rounded) bbox is cached on disk; the second
search over the same area is instant. The bbox is rounded to 0.02° (~2 km)
to maximize cache hits across slightly-different searches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Sequence

from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from .search import Candidate

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 60

# OSM filters whose ways/relations we treat as "likely public access".
PUBLIC_FILTERS = [
    "leisure=park",
    "leisure=nature_reserve",
    "boundary=protected_area",
    "boundary=national_park",
    "landuse=recreation_ground",
]

# OSM filters for water bodies — used to exclude candidates whose camera or
# model would otherwise land in a lake, reservoir, or bay.
WATER_FILTERS = [
    "natural=water",
    "landuse=reservoir",
    "natural=bay",
    "natural=strait",
    "waterway=riverbank",
]


def _bbox_cache_key(
    bbox: tuple[float, float, float, float], salt: str = "",
) -> str:
    """Round bbox to a coarse grid so neighbouring searches reuse one cache."""
    rounded = tuple(round(v, 2) for v in bbox)
    return hashlib.sha1((salt + repr(rounded)).encode()).hexdigest()[:16]


def _build_overpass_query(
    bbox: tuple[float, float, float, float], filters: Sequence[str],
) -> str:
    # Overpass bbox order is (south, west, north, east).
    w, s, e, n = bbox
    bbox_s = f"{s},{w},{n},{e}"
    parts = []
    for filt in filters:
        parts.append(f"way[{filt}]({bbox_s})")
        parts.append(f"relation[{filt}]({bbox_s})")
    union = ";\n  ".join(parts)
    return f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n(\n  {union};\n);\nout geom;"


def fetch_osm_polygons(
    bbox: tuple[float, float, float, float],
    filters: Sequence[str],
    cache_dir: str | Path,
    cache_prefix: str = "osm",
    overpass_url: str = OVERPASS_URL,
) -> dict | None:
    """Return raw Overpass JSON for `bbox` and `filters`, using disk cache.

    On network failure returns None — caller falls back to no annotation
    / no filtering.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _bbox_cache_key(bbox, salt=cache_prefix)
    cache_path = cache_dir / f"{cache_prefix}_{key}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("ignoring corrupt cache at %s", cache_path)
    query = _build_overpass_query(bbox, filters)
    logger.info("fetching %s polygons for bbox=%s via Overpass", cache_prefix, bbox)
    # Overpass rejects requests with a default python-urllib User-Agent.
    # Identify ourselves per Overpass usage policy
    # (https://operations.osmfoundation.org/policies/api/).
    req = urllib.request.Request(
        overpass_url,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        method="POST",
        headers={
            "User-Agent": "moon-portrait-finder/0.1 "
                          "(+https://github.com/local; personal photography tool)",
            "Accept": "application/json",
        },
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_S) as r:
            payload = r.read()
        logger.info("  Overpass returned %.1f kB in %.1fs",
                    len(payload) / 1024, time.time() - t0)
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Overpass fetch failed for %s; skipping", cache_prefix)
        return None
    try:
        cache_path.write_bytes(payload)
    except Exception:  # noqa: BLE001
        logger.warning("failed to cache Overpass response at %s", cache_path)
    return data


# Back-compat shim — the old name returned PUBLIC_FILTERS results.
def fetch_public_osm(
    bbox: tuple[float, float, float, float],
    cache_dir: str | Path,
    overpass_url: str = OVERPASS_URL,
) -> dict | None:
    return fetch_osm_polygons(
        bbox, PUBLIC_FILTERS, cache_dir,
        cache_prefix="public", overpass_url=overpass_url,
    )


def _polygons_from_overpass(data: dict) -> list[Polygon]:
    """Extract closed polygons from an Overpass JSON response.

    Ways: use their `geometry` directly. Relations: stitch their `outer`
    members. Skips self-intersecting or near-degenerate geometries.
    """
    polys: list[Polygon] = []
    for el in data.get("elements", []):
        kind = el.get("type")
        if kind == "way":
            geom = el.get("geometry") or []
            if len(geom) >= 3:
                coords = [(g["lon"], g["lat"]) for g in geom]
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                _try_add_polygon(polys, coords)
        elif kind == "relation":
            for member in el.get("members", []):
                if member.get("role") != "outer":
                    continue
                geom = member.get("geometry") or []
                if len(geom) >= 3:
                    coords = [(g["lon"], g["lat"]) for g in geom]
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    _try_add_polygon(polys, coords)
    return polys


def _try_add_polygon(out: list[Polygon], coords: list[tuple[float, float]]) -> None:
    if len(coords) < 4:
        return
    try:
        p = Polygon(coords)
        if p.is_valid and not p.is_empty:
            out.append(p)
        else:
            # Common Overpass artifact: self-intersecting border. buffer(0)
            # repairs many of these without changing perceived shape.
            fixed = p.buffer(0)
            if fixed.is_valid and not fixed.is_empty:
                if fixed.geom_type == "Polygon":
                    out.append(fixed)
                elif fixed.geom_type == "MultiPolygon":
                    out.extend(fixed.geoms)
    except Exception:  # noqa: BLE001
        pass


class PublicLandIndex:
    """Fast point-in-polygon lookup over a set of polygons (WGS84 lon/lat).

    Use:
        idx = PublicLandIndex(polygons)
        idx.contains(lat, lon)  -> bool
    """

    def __init__(self, polygons: Sequence[Polygon]):
        self.polygons = [p for p in polygons if p.is_valid and not p.is_empty]
        self.tree = STRtree(self.polygons) if self.polygons else None

    def __len__(self) -> int:
        return len(self.polygons)

    def contains(self, lat: float, lon: float) -> bool:
        if not self.tree:
            return False
        p = Point(lon, lat)  # shapely: x=lon, y=lat
        idxs = self.tree.query(p)
        # shapely 2.x returns array of indices; iterate generically
        for i in idxs:
            if self.polygons[int(i)].contains(p):
                return True
        return False

    def contains_batch(
        self, lats: Sequence[float], lons: Sequence[float],
    ) -> list[bool]:
        if not self.tree:
            return [False] * len(lats)
        return [self.contains(lat, lon) for lat, lon in zip(lats, lons)]


def build_index_for_bbox(
    bbox: tuple[float, float, float, float], cache_dir: str | Path,
) -> PublicLandIndex | None:
    """Convenience: fetch Overpass + build index. Returns None on failure."""
    data = fetch_public_osm(bbox, cache_dir)
    if data is None:
        return None
    polys = _polygons_from_overpass(data)
    logger.info("  parsed %d public-land polygons", len(polys))
    return PublicLandIndex(polys)


def annotate(cands: Sequence[Candidate], idx: PublicLandIndex | None) -> int:
    """Set camera_public / model_public on each candidate in place.

    If `idx` is None, leaves the fields as None (no annotation).
    Returns the number of candidates where BOTH points are inside a polygon.
    """
    if idx is None or len(idx) == 0:
        return 0
    both_public = 0
    for c in cands:
        cp = idx.contains(c.camera_lat, c.camera_lon)
        mp = idx.contains(c.model_lat, c.model_lon)
        c.camera_public = cp
        c.model_public = mp
        if cp and mp:
            both_public += 1
    return both_public


# ---- water-body filtering ---------------------------------------------------


def build_water_index_for_bbox(
    bbox: tuple[float, float, float, float], cache_dir: str | Path,
) -> PublicLandIndex | None:
    """Fetch OSM water polygons (lakes, reservoirs, bays) and build a
    point-in-polygon index. Reuses PublicLandIndex since it's just a
    polygon-membership test — semantics are unrelated to public access.
    Returns None on Overpass failure (callers should treat as "no filter").
    """
    data = fetch_osm_polygons(bbox, WATER_FILTERS, cache_dir,
                              cache_prefix="water")
    if data is None:
        return None
    polys = _polygons_from_overpass(data)
    logger.info("  parsed %d water polygons", len(polys))
    return PublicLandIndex(polys)


def filter_out_water(
    cands: Sequence[Candidate],
    water_idx: PublicLandIndex | None,
) -> tuple[list[Candidate], int]:
    """Return (kept, dropped_count). A candidate is dropped if either its
    camera or model lat/lon falls inside any water polygon. If water_idx
    is None or empty, returns the input list unchanged.
    """
    if water_idx is None or len(water_idx) == 0:
        return list(cands), 0
    kept: list[Candidate] = []
    dropped = 0
    for c in cands:
        if (water_idx.contains(c.camera_lat, c.camera_lon)
                or water_idx.contains(c.model_lat, c.model_lon)):
            dropped += 1
            continue
        kept.append(c)
    return kept, dropped
