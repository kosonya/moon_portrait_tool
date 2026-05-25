"""DEM loading + access.

Loads a chunk of USGS 3DEP 1/3 arc-second (~10 m) DEM tiles into a single
metric grid (UTM 10N, NAD83) for the Bay Area. Tiles are read lazily via
GDAL /vsicurl/ and reprojected on the fly with rasterio's WarpedVRT.

Why UTM: search math is much simpler in meters. UTM 10N is appropriate for
all of central California within ~3° of central meridian 123°W. Distortion
across our 100 km radius is < 0.04 % — negligible.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge as rasterio_merge
from rasterio.transform import rowcol, xy
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

logger = logging.getLogger(__name__)

TNM_API = (
    "https://tnmaccess.nationalmap.gov/api/v1/products"
    "?datasets=National%20Elevation%20Dataset%20(NED)%201/3%20arc-second"
    "&bbox={bbox}&outputFormat=JSON"
)

UTM10N = "EPSG:32610"
WGS84 = "EPSG:4326"


@dataclasses.dataclass
class TerrainGrid:
    """A 2D elevation grid in UTM 10N meters.

    Attributes:
        elev:    float32 array shape (rows, cols), elevation in meters above NAVD88.
        x0, y0:  UTM coordinates of the top-left corner of cell (0, 0).
        res:     cell size in meters (square cells assumed).
        nodata:  value used to mark unknown elevation (e.g., outside coverage).
    """
    elev: np.ndarray
    x0: float
    y0: float
    res: float
    nodata: float = -9999.0

    @property
    def rows(self) -> int:
        return self.elev.shape[0]

    @property
    def cols(self) -> int:
        return self.elev.shape[1]

    # ---- coordinate conversion ----------------------------------------------

    def xy_to_rc(self, x: float, y: float) -> tuple[float, float]:
        """UTM (x, y) -> (row, col), allowing fractional indices."""
        col = (x - self.x0) / self.res
        row = (self.y0 - y) / self.res
        return row, col

    def rc_to_xy(self, row: float, col: float) -> tuple[float, float]:
        """(row, col) -> UTM (x, y) of cell center."""
        x = self.x0 + (col + 0.5) * self.res
        y = self.y0 - (row + 0.5) * self.res
        return x, y

    def sample(self, x: float | np.ndarray, y: float | np.ndarray) -> np.ndarray:
        """Bilinear sample of elevation at UTM (x, y). Returns nodata outside grid."""
        x = np.atleast_1d(np.asarray(x, dtype=np.float64))
        y = np.atleast_1d(np.asarray(y, dtype=np.float64))
        col = (x - self.x0) / self.res - 0.5
        row = (self.y0 - y) / self.res - 0.5
        out = np.full(x.shape, self.nodata, dtype=np.float32)

        c0 = np.floor(col).astype(np.int64)
        r0 = np.floor(row).astype(np.int64)
        fc = (col - c0).astype(np.float32)
        fr = (row - r0).astype(np.float32)

        valid = (r0 >= 0) & (c0 >= 0) & (r0 + 1 < self.rows) & (c0 + 1 < self.cols)
        if valid.any():
            r0v, c0v = r0[valid], c0[valid]
            frv, fcv = fr[valid], fc[valid]
            e00 = self.elev[r0v,     c0v]
            e01 = self.elev[r0v,     c0v + 1]
            e10 = self.elev[r0v + 1, c0v]
            e11 = self.elev[r0v + 1, c0v + 1]
            top = e00 * (1 - fcv) + e01 * fcv
            bot = e10 * (1 - fcv) + e11 * fcv
            out[valid] = top * (1 - frv) + bot * frv
        return out


# ---- tile fetcher ------------------------------------------------------------


def list_tiles_for_bbox(bbox_wgs84: tuple[float, float, float, float]) -> list[str]:
    """Return list of USGS 3DEP 1/3 arc-second download URLs covering bbox.

    bbox_wgs84: (west, south, east, north) in degrees.
    Returns the latest publication of each unique tile in the bbox.
    """
    bbox_str = ",".join(str(b) for b in bbox_wgs84)
    url = TNM_API.format(bbox=bbox_str)
    with urllib.request.urlopen(url) as r:
        data = json.load(r)
    items = data.get("items", [])
    # Group by tile name; pick latest per tile.
    by_tile: dict[str, dict] = {}
    for it in items:
        title = it.get("title", "")
        # Title format: "USGS 1/3 Arc Second n38w122 YYYYMMDD"
        parts = title.split()
        tile = next((p for p in parts if p.startswith(("n", "s")) and "w" in p), None)
        if not tile:
            continue
        pub = it.get("publicationDate", "")
        if tile not in by_tile or pub > by_tile[tile].get("publicationDate", ""):
            by_tile[tile] = it
    urls = [v["downloadURL"] for v in by_tile.values()]
    logger.info("TNM API returned %d unique tiles for bbox %s", len(urls), bbox_wgs84)
    return urls


def load_terrain(
    bbox_wgs84: tuple[float, float, float, float],
    target_res_m: float = 10.0,
    cache_dir: str | Path = "data/dem_cache",
) -> TerrainGrid:
    """Fetch + reproject DEM for a WGS84 bbox into a UTM 10N TerrainGrid.

    Caches the reprojected grid as a local GeoTIFF keyed by bbox+res. First
    call for a region streams data via /vsicurl/; subsequent calls reuse the
    cache. This avoids downloading full 222 MB tiles every run.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{bbox_wgs84}_{target_res_m}".encode()
    ).hexdigest()[:16]
    cache_path = cache_dir / f"dem_{key}.tif"

    if cache_path.exists():
        logger.info("Using cached DEM at %s", cache_path)
        with rasterio.open(cache_path) as src:
            elev = src.read(1).astype(np.float32)
            nodata = src.nodata if src.nodata is not None else -9999.0
            t = src.transform
            return TerrainGrid(elev=elev, x0=t.c, y0=t.f,
                               res=float(t.a), nodata=float(nodata))

    urls = list_tiles_for_bbox(bbox_wgs84)
    if not urls:
        raise RuntimeError(f"No 3DEP tiles found for bbox {bbox_wgs84}")
    vsi_urls = ["/vsicurl/" + u for u in urls]

    # UTM 10N bounds for the requested WGS84 bbox.
    west, south, east, north = bbox_wgs84
    utm_bounds = transform_bounds(WGS84, UTM10N, west, south, east, north)
    # Snap to multiples of target_res_m.
    res = target_res_m
    x_min = np.floor(utm_bounds[0] / res) * res
    y_min = np.floor(utm_bounds[1] / res) * res
    x_max = np.ceil(utm_bounds[2] / res) * res
    y_max = np.ceil(utm_bounds[3] / res) * res
    out_w = int(round((x_max - x_min) / res))
    out_h = int(round((y_max - y_min) / res))
    dst_transform = rasterio.transform.from_origin(x_min, y_max, res, res)

    # Open each source, wrap in WarpedVRT into UTM 10N at target_res_m,
    # then mosaic.
    vrts = []
    sources = []
    for u in vsi_urls:
        src = rasterio.open(u)
        sources.append(src)
        vrt = WarpedVRT(
            src,
            crs=UTM10N,
            transform=dst_transform,
            width=out_w,
            height=out_h,
            resampling=Resampling.bilinear,
        )
        vrts.append(vrt)
    try:
        mosaic, mosaic_transform = rasterio_merge(vrts, res=(res, res), nodata=-9999.0)
        elev = mosaic[0].astype(np.float32)
    finally:
        for v in vrts: v.close()
        for s in sources: s.close()

    # Persist cache.
    with rasterio.open(
        cache_path, "w",
        driver="GTiff", height=elev.shape[0], width=elev.shape[1],
        count=1, dtype="float32",
        crs=UTM10N, transform=mosaic_transform, nodata=-9999.0,
        compress="deflate", tiled=True,
    ) as dst:
        dst.write(elev, 1)

    t = mosaic_transform
    return TerrainGrid(elev=elev, x0=t.c, y0=t.f, res=float(t.a), nodata=-9999.0)
