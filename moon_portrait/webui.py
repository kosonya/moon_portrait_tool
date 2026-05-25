"""Localhost Flask UI for the moon-portrait location finder.

Run:
    python -m moon_portrait.webui  --data-dir data --results-dir results

Opens at http://127.0.0.1:5000/. The form lets you tune every search
constraint; on submit, the search runs synchronously and renders the
Folium map inline. Each search's full outputs (CSV / GeoJSON / map +
input parameters) are saved under results/<run_id>/ and persist across
server restarts.

Persistence model
-----------------
Each run writes results/<run_id>/meta.json containing:
  - params:    the form dict used for the run (so the form can be repopulated)
  - summary:   n_raw / n_unique / n_pairs / elapsed_s / completed_at_utc

The sidebar lists all directories under results/ that contain a meta.json,
plus any "legacy" directories without meta.json (these can still be opened
to view their map.html but won't repopulate the form).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import (Flask, Response, redirect, render_template_string, request,
                   send_from_directory, url_for)

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
          border-right: 1px solid #ddd; overflow-y: auto; max-height: 100vh; }
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

  .runs-list { margin-top: 6px; }
  .run-row { display: flex; align-items: center; justify-content: space-between;
             padding: 6px 8px; border-radius: 3px; font-size: 12px;
             border: 1px solid transparent; }
  .run-row:hover { background: #ebebee; }
  .run-row.active { background: #d6e7f5; border-color: #aac8e1; }
  .run-row.legacy { color: #999; font-style: italic; }
  .run-row a { color: inherit; text-decoration: none; flex: 1; }
  .run-row .meta { color: #777; font-size: 11px; }
  .run-row .pairs { font-weight: bold; color: #1f78b4; }
  .run-row.zero .pairs { color: #cc6600; }
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
    </form>

    {% if current_run %}
      <h2>Current run</h2>
      <div class="help">
        Run <code>{{ current_run.run_id[:8] }}</code>
        {% if current_run.legacy %}
          &mdash; <i>legacy (no saved parameters)</i><br>
          {% if current_run.n_unique %}{{ current_run.n_unique }} candidate(s) in CSV.<br>{% endif %}
        {% else %}
          &mdash; {{ current_run.n_raw }} raw → {{ current_run.n_unique }} candidates
          across {{ current_run.n_pairs }} (camera, model) pair(s).<br>
          {% if current_run.elapsed_s %}
            Search took {{ "%.1f"|format(current_run.elapsed_s) }} s.<br>
          {% endif %}
        {% endif %}
        <a href="/download/{{ current_run.run_id }}/candidates.csv">CSV</a> ·
        <a href="/download/{{ current_run.run_id }}/candidates.geojson">GeoJSON</a> ·
        <a href="/download/{{ current_run.run_id }}/map.html" target="_blank">map ↗</a>
      </div>
    {% endif %}

    <h2>Previous runs ({{ runs|length }})</h2>
    {% if runs %}
      <div class="runs-list">
        {% for r in runs %}
          <div class="run-row {% if current_run and r.run_id == current_run.run_id %}active{% endif %}
                              {% if r.legacy %}legacy{% endif %}
                              {% if r.summary and r.summary.n_pairs == 0 %}zero{% endif %}">
            <a href="/load/{{ r.run_id }}">
              <div>
                {% if r.legacy %}
                  <code>{{ r.run_id[:8] }}</code> — <i>no metadata</i>
                {% else %}
                  <span class="pairs">{{ r.summary.n_pairs }}</span> pair(s)
                  · {{ r.summary.n_unique }} timings
                {% endif %}
              </div>
              <div class="meta">
                {% if r.summary %}{{ r.summary.completed_at_local }}{% else %}{{ r.mtime_local }}{% endif %}
                {% if r.summary %}· {{ "%.1f"|format(r.summary.elapsed_s) }} s{% endif %}
              </div>
            </a>
          </div>
        {% endfor %}
      </div>
    {% else %}
      <p class="help">No previous runs yet. Submit a search to create one.</p>
    {% endif %}
  </aside>
  <main>
    {% if current_run %}
      <div class="topbar"
           style="{% if current_run.n_pairs == 0 %}background:#cc6600;{% endif %}">
        <span>Run <code>{{ current_run.run_id[:8] }}</code> &mdash;
          {% if current_run.legacy %}
            (legacy run — no parameters saved; form left unchanged)
          {% elif current_run.n_pairs == 0 %}
            <b>No candidates found.</b> Widen the constraints (date range,
            distance, altitude band, or alt-match tolerance) and search again.
          {% else %}
            <span class="stat">{{ current_run.n_pairs }}</span> unique
            (camera, model) pair(s),
            <span class="stat">{{ current_run.n_unique }}</span> timing
            opportunit{{ "ies" if current_run.n_unique != 1 else "y" }}
            {% if current_run.elapsed_s %}
              ({{ "%.1f"|format(current_run.elapsed_s) }} s)
            {% endif %}.
          {% endif %}
        </span>
        <a href="/download/{{ current_run.run_id }}/map.html" target="_blank"
           style="color:#cce4f6;">open map in new tab ↗</a>
      </div>
      <iframe class="map-frame" src="/download/{{ current_run.run_id }}/map.html"></iframe>
    {% else %}
      <div class="empty-state">Configure constraints on the left and click Search,
        or select a previous run.</div>
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


# ---- run-history helpers ----------------------------------------------------


def _list_runs(results_dir: Path) -> list[dict]:
    """Enumerate run directories. Each entry has:
        run_id: str
        mtime: float (unix)
        mtime_local: str
        legacy: bool  (True iff no meta.json present)
        summary: dict | None  (n_raw, n_unique, n_pairs, elapsed_s,
                               completed_at_local) when present
    Sorted newest first by mtime (or summary.completed_at if available).
    """
    if not results_dir.is_dir():
        return []
    runs: list[dict] = []
    for d in results_dir.iterdir():
        if not d.is_dir():
            continue
        # Require at least one of map.html / candidates.csv / meta.json so
        # we don't list a random subdirectory.
        if not any((d / f).exists() for f in
                   ("map.html", "candidates.csv", "meta.json")):
            continue
        meta_path = d / "meta.json"
        summary = None
        legacy = True
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                summary = meta.get("summary") or {}
                legacy = False
                if summary.get("completed_at_utc"):
                    try:
                        ts = datetime.fromisoformat(summary["completed_at_utc"])
                        summary["completed_at_local"] = ts.astimezone().strftime(
                            "%Y-%m-%d %H:%M")
                    except ValueError:
                        summary["completed_at_local"] = summary["completed_at_utc"]
            except Exception:  # noqa: BLE001
                log.exception("failed to read %s", meta_path)
        mtime = d.stat().st_mtime
        runs.append(dict(
            run_id=d.name,
            mtime=mtime,
            mtime_local=datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            legacy=legacy,
            summary=summary,
        ))
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def _read_meta(results_dir: Path, run_id: str) -> dict | None:
    """Return the parsed meta.json for a run, or None if missing/invalid."""
    meta_path = results_dir / run_id / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text())
    except Exception:  # noqa: BLE001
        log.exception("failed to parse %s", meta_path)
        return None


def _run_record_from_meta(meta: dict, run_id: str) -> dict:
    """Build the dict that the template's `current_run` consumes."""
    s = meta.get("summary") or {}
    return dict(
        run_id=run_id,
        n_raw=s.get("n_raw", 0),
        n_unique=s.get("n_unique", 0),
        n_pairs=s.get("n_pairs", 0),
        elapsed_s=s.get("elapsed_s"),
        legacy=False,
    )


def _legacy_run_record(run_id: str, results_dir: Path) -> dict:
    """Best-effort summary when meta.json is missing (count CSV rows)."""
    csv_path = results_dir / run_id / "candidates.csv"
    n_unique = 0
    if csv_path.is_file():
        try:
            with open(csv_path) as f:
                n_unique = max(0, sum(1 for _ in f) - 1)
        except Exception:  # noqa: BLE001
            pass
    return dict(
        run_id=run_id, n_raw=None, n_unique=n_unique, n_pairs=None,
        elapsed_s=None, legacy=True,
    )


# ---- app factory ------------------------------------------------------------


def create_app(data_dir: Path, results_dir: Path) -> Flask:
    app = Flask(__name__)
    data_dir = Path(data_dir).resolve()
    results_dir = Path(results_dir).resolve()
    log.info("data dir:    %s", data_dir)
    log.info("results dir: %s", results_dir)
    # current_run is what the right pane displays; survives within a server
    # process but is also derivable from disk so the user can pick it again
    # after a restart.
    state = {"current_run": None, "form": dict(DEFAULTS)}

    def render():
        runs = _list_runs(results_dir)
        return render_template_string(
            INDEX_HTML, form=state["form"],
            current_run=state["current_run"], runs=runs,
        )

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            form = {k: request.form.get(k, v) for k, v in DEFAULTS.items()}
            state["form"] = form
            try:
                run = _do_search(form, data_dir, results_dir)
            except Exception as e:
                log.exception("search failed")
                return f"<pre>Search failed: {e}</pre>", 500
            state["current_run"] = run
        return render()

    @app.route("/load/<run_id>")
    def load_run(run_id):
        run_dir = (results_dir / run_id).resolve()
        try:
            run_dir.relative_to(results_dir)
        except ValueError:
            return Response("forbidden", status=403)
        if not run_dir.is_dir():
            return Response(f"run not found: {run_id}", status=404)

        meta = _read_meta(results_dir, run_id)
        if meta is None:
            # Legacy run — show its map but DON'T mutate the form fields.
            state["current_run"] = _legacy_run_record(run_id, results_dir)
        else:
            state["current_run"] = _run_record_from_meta(meta, run_id)
            params = meta.get("params") or {}
            # Only overwrite keys we know about, in case meta.json contains
            # extras from a newer version or a stale schema.
            for k in DEFAULTS:
                if k in params:
                    state["form"][k] = str(params[k])
        return redirect(url_for("index"))

    @app.route("/download/<run_id>/<path:fname>")
    def download(run_id, fname):
        run_dir = (results_dir / run_id).resolve()
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
        return send_from_directory(str(run_dir), fname)

    return app


# ---- search execution -------------------------------------------------------


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
    write_map(dedup, out / "map.html")

    summary = dict(
        n_raw=len(raw), n_unique=len(dedup), n_pairs=len(pairs),
        elapsed_s=elapsed,
        completed_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    meta = dict(version=1, params=form, summary=summary)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))

    return dict(run_id=run_id, n_raw=len(raw), n_unique=len(dedup),
                n_pairs=len(pairs), elapsed_s=elapsed, legacy=False)


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
