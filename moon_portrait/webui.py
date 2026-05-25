"""Localhost Flask UI for the moon-portrait location finder.

Run:
    python -m moon_portrait.webui  --data-dir data --results-dir results

Opens at http://127.0.0.1:5000/. The form lets you tune every search
constraint; on submit, the search runs synchronously and renders the
Folium map inline. Each search's full outputs (CSV / GeoJSON / map) are
saved under results/<run_id>/ for download.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, render_template_string, request, send_from_directory

from .astro import AstroEngine
from .dem import load_terrain
from .output import write_csv, write_geojson, write_map, cluster_by_pair
from .search import SearchConfig, deduplicate, search_windows


log = logging.getLogger("moon_portrait.webui")


INDEX_HTML = """
<!doctype html>
<title>Moon-portrait location finder</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 0; display: flex; min-height: 100vh; }
  aside { width: 360px; padding: 18px 20px; background: #f7f7f8;
          border-right: 1px solid #ddd; overflow-y: auto; }
  main { flex: 1; display: flex; flex-direction: column; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  h2 { font-size: 13px; margin: 16px 0 6px; color: #555; text-transform: uppercase;
       letter-spacing: 0.05em; }
  label { display: block; font-size: 12px; color: #444; margin: 8px 0 2px; }
  input[type=text], input[type=number], input[type=date] {
    width: 100%; padding: 4px 6px; font-size: 13px; box-sizing: border-box;
    border: 1px solid #ccc; border-radius: 3px; }
  .row { display: flex; gap: 8px; }
  .row > * { flex: 1; }
  button { width: 100%; padding: 10px; font-size: 14px; margin-top: 16px;
           background: #1f78b4; color: white; border: none; border-radius: 3px;
           cursor: pointer; }
  button:hover { background: #155a8a; }
  .help { font-size: 11px; color: #888; margin-top: 2px; }
  .map-frame { flex: 1; border: none; }
  .topbar { padding: 8px 16px; background: #1f78b4; color: white;
            display: flex; justify-content: space-between; align-items: center;
            font-size: 13px; }
  .topbar a { color: #cce4f6; }
  .stat { font-weight: bold; }
  .empty-state { display: flex; align-items: center; justify-content: center;
                 height: 100%; color: #888; font-size: 14px; }
</style>
<body>
  <aside>
    <form method="post" action="/">
      <h1>🌕 Moon portrait finder</h1>
      <p class="help">Searches USGS 3DEP 10 m terrain for camera/model pairs
        where the full Moon aligns with the model at the camera's eye.</p>

      <h2>Region</h2>
      <label>BBox (west,south,east,north WGS84)</label>
      <input name="bbox" value="{{ form.bbox }}" required>
      <div class="row">
        <div>
          <label>Observer lat</label>
          <input name="observer_lat" value="{{ form.observer_lat }}" type="number" step="0.0001">
        </div>
        <div>
          <label>Observer lon</label>
          <input name="observer_lon" value="{{ form.observer_lon }}" type="number" step="0.0001">
        </div>
      </div>
      <div class="row">
        <div>
          <label>Start (UTC)</label>
          <input name="start" value="{{ form.start }}" type="date">
        </div>
        <div>
          <label>End (UTC)</label>
          <input name="end" value="{{ form.end }}" type="date">
        </div>
      </div>

      <h2>Distance constraint</h2>
      <div class="row">
        <div>
          <label>Min (m)</label>
          <input name="d_min" value="{{ form.d_min }}" type="number" step="10">
        </div>
        <div>
          <label>Max (m)</label>
          <input name="d_max" value="{{ form.d_max }}" type="number" step="10">
        </div>
      </div>

      <h2>Altitude / sun constraints</h2>
      <div class="row">
        <div>
          <label>Moon alt min °</label>
          <input name="alt_min" value="{{ form.alt_min }}" type="number" step="0.5">
        </div>
        <div>
          <label>Moon alt max °</label>
          <input name="alt_max" value="{{ form.alt_max }}" type="number" step="0.5">
        </div>
      </div>
      <label>Sun alt max ° (≤ -10 = astro twilight)</label>
      <input name="sun_alt_max" value="{{ form.sun_alt_max }}" type="number" step="0.5">
      <label>Phase tolerance ° (from full moon)</label>
      <input name="phase_tol" value="{{ form.phase_tol }}" type="number" step="0.5">
      <label>Alt match tolerance °</label>
      <input name="alt_tol" value="{{ form.alt_tol }}" type="number" step="0.05">

      <h2>Performance / resolution</h2>
      <label>DEM resolution (m) — coarser is faster</label>
      <input name="dem_res" value="{{ form.dem_res }}" type="number" step="5">
      <label>Sample step (min)</label>
      <input name="sample_step_min" value="{{ form.sample_step_min }}" type="number" step="1">
      <label>Output dedup snap (m)</label>
      <input name="snap_m" value="{{ form.snap_m }}" type="number" step="5">

      <button type="submit">Search</button>
      {% if last_run %}
        <h2>Last run</h2>
        <div class="help">
          {{ last_run.n_raw }} raw → {{ last_run.n_unique }} unique candidates
          across {{ last_run.n_pairs }} (camera, model) pairs.<br>
          Search took {{ "%.1f"|format(last_run.elapsed_s) }} s.<br>
          <a href="/download/{{ last_run.run_id }}/candidates.csv">CSV</a> ·
          <a href="/download/{{ last_run.run_id }}/candidates.geojson">GeoJSON</a> ·
          <a href="/download/{{ last_run.run_id }}/map.html">map</a>
        </div>
      {% endif %}
    </form>
  </aside>
  <main>
    {% if last_run %}
      <div class="topbar"
           style="{% if last_run.n_pairs == 0 %}background:#cc6600;{% endif %}">
        <span>Run <code>{{ last_run.run_id[:8] }}</code> &mdash;
          {% if last_run.n_pairs == 0 %}
            <b>No candidates found.</b> Widen the constraints (date range,
            distance, altitude band, or alt-match tolerance) and search again.
          {% else %}
            <span class="stat">{{ last_run.n_pairs }}</span> unique
            (camera, model) pair(s),
            <span class="stat">{{ last_run.n_unique }}</span> timing
            opportunit{{ "ies" if last_run.n_unique != 1 else "y" }}
            ({{ "%.1f"|format(last_run.elapsed_s) }} s).
          {% endif %}
        </span>
        <a href="/download/{{ last_run.run_id }}/map.html" target="_blank"
           style="color:#cce4f6;">open map in new tab ↗</a>
      </div>
      <iframe class="map-frame" src="/download/{{ last_run.run_id }}/map.html"></iframe>
    {% else %}
      <div class="empty-state">Configure constraints on the left and click Search.</div>
    {% endif %}
  </main>
</body>
"""


DEFAULTS = dict(
    bbox="-121.96,37.46,-121.78,37.56",
    observer_lat="37.5", observer_lon="-121.87",
    start="2026-05-24", end="2026-08-01",
    d_min="250", d_max="500",
    alt_min="3", alt_max="20",
    sun_alt_max="-10", phase_tol="15", alt_tol="0.15",
    dem_res="30", sample_step_min="10", snap_m="75",
)


def create_app(data_dir: Path, results_dir: Path) -> Flask:
    app = Flask(__name__)
    # send_from_directory wants an absolute path; without this, relative
    # paths are resolved against Flask's CWD which may not match yours.
    data_dir = Path(data_dir).resolve()
    results_dir = Path(results_dir).resolve()
    log.info("data dir:    %s", data_dir)
    log.info("results dir: %s", results_dir)
    state = {"last_run": None}

    @app.route("/", methods=["GET", "POST"])
    def index():
        form = dict(DEFAULTS)
        if request.method == "POST":
            form.update({k: request.form.get(k, v) for k, v in DEFAULTS.items()})
            try:
                run = _do_search(form, data_dir, results_dir)
                state["last_run"] = run
            except Exception as e:
                log.exception("search failed")
                return f"<pre>Search failed: {e}</pre>", 500
        return render_template_string(INDEX_HTML, form=form,
                                       last_run=state["last_run"])

    @app.route("/download/<run_id>/<path:fname>")
    def download(run_id, fname):
        run_dir = (results_dir / run_id).resolve()
        # safety: must remain under results_dir
        try:
            run_dir.relative_to(results_dir)
        except ValueError:
            return Response("forbidden", status=403)
        target = run_dir / fname
        if not target.is_file():
            return Response(
                f"file not found: {target}\n"
                f"(run dir exists: {run_dir.is_dir()})\n"
                f"(contents: {list(p.name for p in run_dir.iterdir()) if run_dir.is_dir() else 'n/a'})",
                status=404, mimetype="text/plain",
            )
        # send_from_directory needs str on some Werkzeug versions
        return send_from_directory(str(run_dir), fname)

    return app


def _do_search(form: dict, data_dir: Path, results_dir: Path) -> dict:
    bbox = tuple(float(x) for x in form["bbox"].split(","))
    assert len(bbox) == 4
    t0 = datetime.fromisoformat(form["start"]).replace(tzinfo=timezone.utc)
    t1 = datetime.fromisoformat(form["end"]).replace(tzinfo=timezone.utc)

    grid = load_terrain(bbox, float(form["dem_res"]),
                        cache_dir=data_dir / "dem_cache")
    eng = AstroEngine(float(form["observer_lat"]), float(form["observer_lon"]),
                      ephem_dir=data_dir)
    windows = eng.lunar_windows(
        t0, t1,
        alt_min_deg=float(form["alt_min"]),
        alt_max_deg=float(form["alt_max"]),
        sun_alt_max_deg=float(form["sun_alt_max"]),
        phase_tolerance_deg=float(form["phase_tol"]),
        sample_step_minutes=float(form["sample_step_min"]),
    )
    cfg = SearchConfig(
        d_min_m=float(form["d_min"]), d_max_m=float(form["d_max"]),
        alt_tol_deg=float(form["alt_tol"]),
        dedup_xy_snap_m=float(form["snap_m"]),
    )
    t0_s = time.time()
    raw = search_windows(grid, windows, cfg)
    dedup = deduplicate(raw, cfg)
    elapsed = time.time() - t0_s

    pairs = cluster_by_pair(dedup, snap_m=float(form["snap_m"]))
    dedup.sort(key=lambda c: (-c.distance_m,
                              abs(c.alt_actual_deg - c.alt_required_deg)))

    run_id = uuid4().hex
    out = results_dir / run_id
    out.mkdir(parents=True, exist_ok=True)
    write_csv(dedup, out / "candidates.csv")
    write_geojson(dedup, out / "candidates.geojson")
    # Don't force a center — fit_bounds in write_map will frame the candidates.
    write_map(dedup, out / "map.html")
    return dict(run_id=run_id, n_raw=len(raw), n_unique=len(dedup),
                n_pairs=len(pairs), elapsed_s=elapsed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(args.data_dir)
    results_dir = Path(args.results_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(data_dir, results_dir)
    log.info("serving on http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
