# syntax=docker/dockerfile:1
#
# Moon-portrait finder — production-style container for NAS deployment.
#
# Image base: python:3.12-slim. The geo stack (rasterio, shapely, pyproj)
# ships with bundled native binaries via manylinux wheels, so no system
# GDAL/GEOS/PROJ packages are required.

FROM python:3.12-slim

# tini    = tiny init; forwards SIGTERM so `docker stop` exits cleanly.
# curl    = used by HEALTHCHECK below.
# libexpat1 = pulled in transitively by rasterio's bundled GDAL via
#             libexpat.so.1; python:3.12-slim strips it out.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl libexpat1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pip layer caches separately from source so code edits don't reinstall deps.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY moon_portrait/ ./moon_portrait/

# Default mount points. Override the host side via docker-compose / -v.
#   /app/results  -- run outputs (CSV, map.html, meta.json, name.txt)
#                    BIND-MOUNT to a Google Drive-synced folder on the host.
#   /app/data     -- DEM cache, OSM Overpass cache, JPL ephemeris.
#                    Persist across restarts but NO need to sync.
RUN mkdir -p /app/results /app/data

EXPOSE 5000

# /status is cheap (no DB; just reads in-memory dict). 200 means alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/status >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "moon_portrait.webui", \
     "--host", "0.0.0.0", "--port", "5000", \
     "--data-dir", "/app/data", \
     "--results-dir", "/app/results"]
