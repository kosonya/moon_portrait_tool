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

Each run may also have a results/<run_id>/name.txt with a user-chosen
display name (set via inline editing in the sidebar). This is independent
of meta.json — legacy runs can be named too.

The sidebar lists all directories under results/ that contain a meta.json,
plus any "legacy" directories without meta.json (these can still be opened
to view their map.html but won't repopulate the form).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, send_from_directory, url_for)

from .astro import AstroEngine
from .dem import load_terrain
from .output import (cluster_by_pair, write_blank_map, write_csv,
                      write_geojson, write_map)
from . import public_land
from .search import (Candidate, SearchCancelled, SearchConfig, deduplicate,
                     iter_search_windows)


log = logging.getLogger("moon_portrait.webui")


INDEX_HTML = """
<!doctype html>
<title>Moon-portrait location finder</title>
<style>
  /* Pin viewport: without these, a tall sidebar makes the body grow past
     100vh, which pushes the iframe (and the legend inside it) below the
     visible window. */
  html, body { margin: 0; padding: 0; height: 100vh; overflow: hidden; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         display: flex; }
  aside { width: 360px; padding: 18px 20px; background: #f7f7f8;
          border-right: 1px solid #ddd; overflow-y: auto;
          height: 100%; flex-shrink: 0; box-sizing: border-box; }
  /* min-height/min-width: 0 on flex children prevents content from forcing
     the flex item to grow past its allocated space. */
  main { flex: 1; display: flex; flex-direction: column;
         min-height: 0; min-width: 0; }
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
  .map-frame { flex: 1; border: none; min-height: 0; }
  .topbar { padding: 8px 16px; background: #1f78b4; color: white;
            display: flex; justify-content: space-between; align-items: center;
            font-size: 13px; }
  .topbar a { color: #cce4f6; }
  .stat { font-weight: bold; }
  .empty-state { display: flex; align-items: center; justify-content: center;
                 height: 100%; color: #888; font-size: 14px; }

  .runs-list { margin-top: 6px; }
  .run-row { display: block; padding: 6px 8px; border-radius: 3px;
             font-size: 12px; border: 1px solid transparent;
             margin-bottom: 2px; }
  .run-row:hover { background: #ebebee; }
  .run-row.active { background: #d6e7f5; border-color: #aac8e1; }
  .run-row.legacy .run-meta { font-style: italic; color: #999; }
  .run-row a.run-load { color: inherit; text-decoration: none; display: block; }
  .run-meta { color: #777; font-size: 11px; }
  .pairs { font-weight: bold; color: #1f78b4; }
  .run-row.zero .pairs { color: #cc6600; }

  .run-name-line { display: flex; align-items: center; gap: 4px; }
  .run-name { flex: 1; padding: 1px 4px; border-radius: 2px;
              border: 1px solid transparent; outline: none;
              font-weight: 500; color: #222; min-height: 16px;
              white-space: pre-wrap; word-break: break-word; }
  .run-name[contenteditable="true"]:hover { border-color: #ccc;
                                             background: white; cursor: text; }
  .run-name[contenteditable="true"]:focus { border-color: #1f78b4;
                                             background: white; }
  .run-name.placeholder { color: #999; font-weight: 400; }
  .rename-btn { background: none; border: none; color: #aaa;
                cursor: pointer; padding: 0 4px; font-size: 13px;
                width: auto; margin: 0; }
  .rename-btn:hover { color: #1f78b4; background: none; }
  .rename-status { font-size: 10px; color: #888; margin-left: 4px; }
  .rename-status.error { color: #c33; }
  .rename-status.ok { color: #2a8; }

  .input-with-btn { display: flex; gap: 4px; align-items: stretch; }
  .input-with-btn > input { flex: 1; }
  .input-with-btn > button { width: auto; margin: 0; padding: 4px 10px;
                              font-size: 11px; white-space: nowrap;
                              background: #e6eef5; color: #1f78b4;
                              border: 1px solid #c0d3e0; }
  .input-with-btn > button:hover { background: #d6e7f5; color: #155a8a; }

  .search-status { padding: 8px 14px; background: #fffbe6;
                   border-top: 1px solid #e0d59a;
                   font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                   display: flex; align-items: center; gap: 12px;
                   flex-wrap: wrap; min-height: 22px; }
  .search-status.done   { background: #e8f4ea; border-color: #b3d8b8; }
  .search-status.cancelled { background: #fbe7d2; border-color: #d8a878; }
  .search-status.error  { background: #fde2e2; border-color: #d89090; }
  .search-status.idle   { display: none; }
  .search-status .phase { font-weight: 600; text-transform: capitalize; }
  .search-status .msg   { flex: 1; color: #555; }
  .search-status .elapsed { color: #888; font-variant-numeric: tabular-nums; }
  .search-status button, .search-status a.btn {
    padding: 4px 10px; font-size: 11px; border-radius: 3px;
    text-decoration: none; border: 1px solid #aaa; background: white;
    cursor: pointer; color: #333;
  }
  .search-status button.stop { color: #c33; border-color: #c33; }
  .search-status button.stop:hover { background: #c33; color: white; }
  .search-status a.btn.view { color: #1f78b4; border-color: #1f78b4; }
  .search-status a.btn.view:hover { background: #1f78b4; color: white; }
</style>
<body>
  <aside>
    <form method="post" action="/">
      <h1>🌕 Moon portrait finder</h1>
      <p class="help">Searches USGS 3DEP 10 m terrain for camera/model pairs
        where the full Moon aligns with the model at the camera's eye.</p>

      <h2>Region</h2>
      <label>BBox (west,south,east,north WGS84)</label>
      <div class="input-with-btn">
        <input name="bbox" value="{{ form.bbox }}" required>
        <button type="button" onclick="useCurrentMapView()"
                title="Set bbox to the area currently visible on the map, and center observer there">
          use map view
        </button>
      </div>
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
      <div class="help">
        Reference point for the astronomical math (Moon altitude / azimuth,
        Sun altitude). At lunar distance, parallax across 100&nbsp;km is
        ~0.015° &mdash; well below the alt-match tolerance &mdash; so anywhere
        inside the bbox is fine. <em>Use map view</em> centers it on the bbox.
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
      <label>Sun altitude range ° (sun_alt must be between min and max)</label>
      <div class="row">
        <div>
          <label>Sun alt min</label>
          <input name="sun_alt_min" value="{{ form.sun_alt_min }}" type="number" step="0.5">
        </div>
        <div>
          <label>Sun alt max</label>
          <input name="sun_alt_max" value="{{ form.sun_alt_max }}" type="number" step="0.5">
        </div>
      </div>
      <div class="help">
        Defaults <code>−90</code>&nbsp;to&nbsp;<code>−10</code> = night
        (sun at least 10° below horizon = astro twilight). For daytime
        photos, set min to <code>0</code> and max to <code>90</code>.
      </div>
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
        {% if current_run.name %}<b>{{ current_run.name }}</b><br>{% endif %}
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

    <div id="runs-section">{{ runs_section_html|safe }}</div>

    <script>
      // Inline rename. The .run-name spans become contenteditable on focus.
      // Idempotent: each span is marked once and skipped on subsequent calls,
      // so we can rebind safely after the runs list is swapped in place.
      function setupRunRenameHandlers() {
        const spans = document.querySelectorAll('.run-name');
        spans.forEach(span => {
          if (span.dataset.renameBound === '1') return;
          span.dataset.renameBound = '1';
          span.setAttribute('contenteditable', 'plaintext-only');
          // Some browsers don't support plaintext-only; fall back.
          if (span.contentEditable !== 'plaintext-only') {
            span.setAttribute('contenteditable', 'true');
          }
          let original = (span.classList.contains('placeholder')) ? '' : span.textContent;
          span.addEventListener('focus', () => {
            // Clear placeholder text on first edit so user types into empty.
            if (span.classList.contains('placeholder')) {
              span.textContent = '';
              span.classList.remove('placeholder');
            }
            original = span.textContent;
          });
          span.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); span.blur(); }
            else if (e.key === 'Escape') {
              span.textContent = original || span.dataset.default;
              if (!original) span.classList.add('placeholder');
              span.blur();
            }
          });
          span.addEventListener('blur', async () => {
            const newName = span.textContent.replace(/\\s+/g, ' ').trim();
            if (newName === original) {
              // No change. Restore placeholder state if empty.
              if (!newName) {
                span.textContent = span.dataset.default;
                span.classList.add('placeholder');
              }
              return;
            }
            const statusEl = document.querySelector(
              `.rename-status[data-run-id="${span.dataset.runId}"]`);
            statusEl.textContent = 'saving…';
            statusEl.className = 'rename-status';
            try {
              const r = await fetch(`/rename/${span.dataset.runId}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: newName}),
              });
              if (!r.ok) throw new Error(await r.text() || r.statusText);
              original = newName;
              if (!newName) {
                span.textContent = span.dataset.default;
                span.classList.add('placeholder');
              }
              statusEl.textContent = 'saved';
              statusEl.className = 'rename-status ok';
              setTimeout(() => { statusEl.textContent = ''; }, 1500);
            } catch (err) {
              span.textContent = original || span.dataset.default;
              if (!original) span.classList.add('placeholder');
              statusEl.textContent = 'error: ' + err.message;
              statusEl.className = 'rename-status error';
            }
          });
        });
      }
      window.addEventListener('load', setupRunRenameHandlers);

      // Live-refresh the previous-runs sidebar without reloading the page.
      // Called after a background search transitions to a terminal phase.
      async function refreshRunsList() {
        try {
          const r = await fetch('/runs/snippet', {cache: 'no-store'});
          if (!r.ok) return;
          const html = await r.text();
          const section = document.getElementById('runs-section');
          if (!section) return;
          section.innerHTML = html;
          setupRunRenameHandlers();
        } catch (e) { /* network blip — try again on next status change */ }
      }

      function focusName(runId) {
        const span = document.querySelector(`.run-name[data-run-id="${runId}"]`);
        if (!span) return;
        span.focus();
        // Move caret to end.
        const range = document.createRange();
        range.selectNodeContents(span);
        range.collapse(false);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }

      // ---- search-status polling ---------------------------------------
      // Always poll once on load; if a search is running we poll again
      // every 1.5s. When it finishes we render a "View results" link and
      // stop polling. Survives page reloads — status lives on the server.
      let _pollTimer = null;
      let _wasRunning = false;  // tracks running→not-running transition
      function renderStatus(s) {
        const panel = document.getElementById('search-status');
        const ph    = document.getElementById('ss-phase');
        const msg   = document.getElementById('ss-msg');
        const el    = document.getElementById('ss-elapsed');
        const stop  = document.getElementById('ss-stop');
        const view  = document.getElementById('ss-view');
        if (!s || (s.phase === 'idle' && !s.running)) {
          panel.className = 'search-status idle';
          return;
        }
        // Color the panel by phase
        let cls = 'search-status';
        if (s.phase === 'done')      cls += ' done';
        else if (s.phase === 'cancelled') cls += ' cancelled';
        else if (s.phase === 'error')     cls += ' error';
        panel.className = cls;
        // Text
        ph.textContent = s.phase.replace(/_/g, ' ');
        msg.textContent = s.message || '';
        el.textContent = (s.elapsed_s != null)
            ? `[${s.elapsed_s.toFixed(1)} s]` : '';
        // Controls
        if (s.running) {
          stop.style.display = 'inline-block';
          view.style.display = 'none';
        } else {
          stop.style.display = 'none';
          if (s.run_id && (s.phase === 'done' || s.phase === 'cancelled')) {
            view.style.display = 'inline-block';
            view.href = '/load/' + s.run_id;
            view.textContent = (s.phase === 'cancelled')
                ? 'View partial results' : 'View results';
          } else {
            view.style.display = 'none';
          }
        }
      }
      async function pollStatus() {
        try {
          const r = await fetch('/status', {cache: 'no-store'});
          const s = await r.json();
          renderStatus(s);
          // Detect a running→done transition and refresh the sidebar.
          if (s && s.running) _wasRunning = true;
          if (_wasRunning && s && !s.running) {
            _wasRunning = false;
            refreshRunsList();
          }
          clearTimeout(_pollTimer);
          if (s && s.running) _pollTimer = setTimeout(pollStatus, 1500);
        } catch (e) { /* network blip — try again */ _pollTimer = setTimeout(pollStatus, 3000); }
      }
      async function cancelSearch() {
        if (!confirm('Stop the running search? Partial results will still be saved.'))
          return;
        await fetch('/cancel', {method: 'POST'});
        pollStatus();
      }
      window.addEventListener('load', pollStatus);

      // "use map view" button: copy the current Leaflet bounds into the
      // bbox field and center observer on that bbox. Reaches into the
      // iframe to find the Leaflet map instance.
      function useCurrentMapView() {
        const iframe = document.querySelector('iframe.map-frame');
        if (!iframe || !iframe.contentWindow) {
          alert("No map loaded. Run a search or load a previous run first.");
          return;
        }
        const win = iframe.contentWindow;
        let map = null;
        for (const k in win) {
          if (k.indexOf('map_') === 0 && win[k]
              && typeof win[k].getBounds === 'function') {
            map = win[k]; break;
          }
        }
        if (!map) {
          alert("Couldn't find the map inside the iframe. " +
                "If the map is still loading, wait a moment and try again.");
          return;
        }
        const b = map.getBounds();
        const w = b.getWest().toFixed(4);
        const s = b.getSouth().toFixed(4);
        const e = b.getEast().toFixed(4);
        const n = b.getNorth().toFixed(4);
        document.querySelector('input[name="bbox"]').value = `${w},${s},${e},${n}`;
        const c = b.getCenter();
        document.querySelector('input[name="observer_lat"]').value = c.lat.toFixed(4);
        document.querySelector('input[name="observer_lon"]').value = c.lng.toFixed(4);
      }
    </script>
  </aside>
  <main>
    {% if current_run %}
      <div class="topbar"
           style="{% if current_run.n_pairs == 0 %}background:#cc6600;{% endif %}">
        <span>
          {% if current_run.name %}<b>{{ current_run.name }}</b> · {% endif %}
          Run <code>{{ current_run.run_id[:8] }}</code> &mdash;
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
      <div class="topbar" style="background:#777;">
        <span>No run selected &mdash; pan/zoom the map below, then click
              <b>use map view</b> + <b>Search</b>, or pick a previous run.</span>
      </div>
      <iframe class="map-frame" src="/blank_map.html"></iframe>
    {% endif %}

    <div id="search-status" class="search-status idle">
      <span class="phase" id="ss-phase">idle</span>
      <span class="msg"   id="ss-msg"></span>
      <span class="elapsed" id="ss-elapsed"></span>
      <button class="stop" id="ss-stop" style="display:none"
              onclick="cancelSearch()">Stop</button>
      <a class="btn view" id="ss-view" style="display:none">View results</a>
    </div>
  </main>
</body>
"""


# Rendered both by the initial page load (inlined into INDEX_HTML via
# runs_section_html) and by GET /runs/snippet (for live refresh after a
# background search finishes — JS swaps just this section's HTML).
RUNS_SECTION_TEMPLATE = """
<h2>Previous runs ({{ runs|length }})</h2>
{% if runs %}
  <div class="runs-list">
    {% for r in runs %}
      <div class="run-row {% if current_run and r.run_id == current_run.run_id %}active{% endif %}
                          {% if r.legacy %}legacy{% endif %}
                          {% if r.summary and r.summary.n_pairs == 0 %}zero{% endif %}"
           data-run-id="{{ r.run_id }}">
        <div class="run-name-line">
          <span class="run-name {% if not r.name %}placeholder{% endif %}"
                data-run-id="{{ r.run_id }}"
                data-default="{{ r.run_id[:8] }}"
                title="Click to rename"
          >{{ r.name or r.run_id[:8] }}</span>
          <button class="rename-btn" title="Rename this run"
                  onclick="event.preventDefault(); event.stopPropagation();
                           focusName('{{ r.run_id }}');">✏️</button>
          <span class="rename-status" data-run-id="{{ r.run_id }}"></span>
        </div>
        <a class="run-load" href="/load/{{ r.run_id }}">
          <div>
            {% if r.legacy %}
              <span class="run-meta">no saved parameters</span>
            {% else %}
              <span class="pairs">{{ r.summary.n_pairs }}</span> pair(s)
              · {{ r.summary.n_unique }} timings
            {% endif %}
          </div>
          <div class="run-meta">
            {% if r.summary %}{{ r.summary.completed_at_local }}{% else %}{{ r.mtime_local }}{% endif %}
            {% if r.summary %}· {{ "%.1f"|format(r.summary.elapsed_s) }} s{% endif %}
            · <code>{{ r.run_id[:8] }}</code>
          </div>
        </a>
      </div>
    {% endfor %}
  </div>
{% else %}
  <p class="help">No previous runs yet. Submit a search to create one.</p>
{% endif %}
"""


DEFAULTS = dict(
    bbox="-121.96,37.46,-121.78,37.56",
    observer_lat="37.5", observer_lon="-121.87",
    start="2026-05-24", end="2026-08-01",
    d_min="250", d_max="500",
    alt_min="3", alt_max="20",
    sun_alt_min="-90", sun_alt_max="-10",
    phase_tol="15", alt_tol="0.15",
    dem_res="30", sample_step_min="10", snap_m="75",
)


# ---- run-history helpers ----------------------------------------------------

# Names are persisted as a plain UTF-8 text file alongside the run outputs.
# Using a separate file rather than a meta.json field lets legacy runs (which
# predate meta.json) be named too.
NAME_FILENAME = "name.txt"
MAX_NAME_LEN = 200


def _read_name(run_dir: Path) -> str:
    """Return the user-set name for a run, or '' if not set."""
    p = run_dir / NAME_FILENAME
    if not p.is_file():
        return ""
    try:
        return _normalize_name(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.exception("failed to read %s", p)
        return ""


def _normalize_name(s: str) -> str:
    """Strip control chars (replacing with space), collapse whitespace, cap length.

    Replaces control characters with a space rather than dropping them so an
    embedded \\x07 in "Bay\\x07test" yields "Bay test" rather than "Baytest".
    """
    cleaned = "".join(ch if (ord(ch) >= 0x20) else " " for ch in s)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_NAME_LEN]


def _write_name(run_dir: Path, name: str) -> None:
    """Persist the run's name. Empty string deletes the name file."""
    p = run_dir / NAME_FILENAME
    name = _normalize_name(name)
    if not name:
        if p.exists():
            p.unlink()
        return
    p.write_text(name, encoding="utf-8")


# String that must be present in a map.html generated by the *current*
# rendering code. Bump this when changing the rendered output format —
# loading any older run will then trigger a one-time regeneration.
MAP_VERSION_MARKER = "position: fixed; bottom: 18px"


def _parse_optional_bool(s: str | None) -> bool | None:
    """Parse a CSV cell that might be empty / 'True' / 'False' / 'None'."""
    if s is None or s == "" or s == "None":
        return None
    return s.lower() in ("true", "1", "yes")


def _load_candidates_from_csv(csv_path: Path) -> list[Candidate]:
    """Reconstruct Candidate objects from a saved candidates.csv.

    Optional public-land columns are reconstructed when present, else left
    as None — so the map's public-only toggle is a no-op for runs that
    pre-date the annotation column.
    """
    cands: list[Candidate] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            cands.append(Candidate(
                time_utc=datetime.fromisoformat(r["time_utc"]),
                az_required_deg=float(r["az_required_deg"]),
                alt_required_deg=float(r["alt_required_deg"]),
                az_actual_deg=float(r["az_actual_deg"]),
                alt_actual_deg=float(r["alt_actual_deg"]),
                distance_m=float(r["distance_m"]),
                elev_gain_m=float(r["elev_gain_m"]),
                camera_x=float(r["camera_x"]),
                camera_y=float(r["camera_y"]),
                camera_elev_m=float(r["camera_elev_m"]),
                camera_lat=float(r["camera_lat"]),
                camera_lon=float(r["camera_lon"]),
                model_x=float(r["model_x"]),
                model_y=float(r["model_y"]),
                model_elev_m=float(r["model_elev_m"]),
                model_lat=float(r["model_lat"]),
                model_lon=float(r["model_lon"]),
                moon_phase_deg=float(r["moon_phase_deg"]),
                sun_alt_deg=float(r["sun_alt_deg"]),
                camera_public=_parse_optional_bool(r.get("camera_public")),
                model_public=_parse_optional_bool(r.get("model_public")),
            ))
    return cands


def _maybe_regenerate_map(run_dir: Path, force: bool = False) -> bool:
    """If `run_dir`'s map.html is missing or stale relative to the current
    rendering code, rewrite it from candidates.csv. Returns True on regen.

    A map is considered "stale" if it doesn't contain MAP_VERSION_MARKER —
    that is, it was written by an older version of `output.py`. This lets
    old runs auto-upgrade their visualization the first time you view them
    after a code change, without needing to re-run the search.
    """
    csv_path = run_dir / "candidates.csv"
    map_path = run_dir / "map.html"
    if not csv_path.is_file():
        return False
    if not force and map_path.is_file():
        try:
            head = map_path.read_text(encoding="utf-8", errors="replace")
            if MAP_VERSION_MARKER in head:
                return False  # up to date
        except Exception:  # noqa: BLE001
            log.exception("failed to read %s for staleness check", map_path)
    try:
        cands = _load_candidates_from_csv(csv_path)
    except Exception:  # noqa: BLE001
        log.exception("failed to reload candidates from %s", csv_path)
        return False
    log.info("regenerating map for %s (%d candidates)", run_dir.name, len(cands))
    write_map(cands, map_path)
    return True


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
            name=_read_name(d),
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


def _run_record_from_meta(meta: dict, run_id: str, results_dir: Path) -> dict:
    """Build the dict that the template's `current_run` consumes."""
    s = meta.get("summary") or {}
    return dict(
        run_id=run_id,
        n_raw=s.get("n_raw", 0),
        n_unique=s.get("n_unique", 0),
        n_pairs=s.get("n_pairs", 0),
        elapsed_s=s.get("elapsed_s"),
        legacy=False,
        name=_read_name(results_dir / run_id),
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
        name=_read_name(results_dir / run_id),
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

    def _render_runs_section():
        runs = _list_runs(results_dir)
        return render_template_string(
            RUNS_SECTION_TEMPLATE, runs=runs,
            current_run=state["current_run"],
        )

    def render():
        return render_template_string(
            INDEX_HTML, form=state["form"],
            current_run=state["current_run"],
            runs_section_html=_render_runs_section(),
        )

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            form = {k: request.form.get(k, v) for k, v in DEFAULTS.items()}
            state["form"] = form
            if active_search.is_running():
                return (
                    "<pre>A search is already running. Stop it via the "
                    "panel below the map and try again.</pre>",
                    409,
                )
            try:
                active_search.start(form, data_dir, results_dir)
            except Exception as e:  # noqa: BLE001
                log.exception("failed to start search")
                return f"<pre>Could not start search: {e}</pre>", 500
            return redirect(url_for("index"))
        return render()

    @app.route("/status")
    def status():
        return jsonify(active_search.status())

    @app.route("/cancel", methods=["POST"])
    def cancel():
        ok = active_search.cancel()
        return jsonify({"ok": ok})

    @app.route("/runs/snippet")
    def runs_snippet():
        # Just the previous-runs section, for live refresh from JS without
        # reloading the whole page. The same template that's inlined into
        # the index on initial render.
        return _render_runs_section()

    @app.route("/load/<run_id>")
    def load_run(run_id):
        run_dir = (results_dir / run_id).resolve()
        try:
            run_dir.relative_to(results_dir)
        except ValueError:
            return Response("forbidden", status=403)
        if not run_dir.is_dir():
            return Response(f"run not found: {run_id}", status=404)

        # Auto-upgrade map.html if it predates the current rendering style.
        # Pass ?regenerate=1 to force regeneration even if it looks current.
        force = request.args.get("regenerate") == "1"
        _maybe_regenerate_map(run_dir, force=force)

        meta = _read_meta(results_dir, run_id)
        if meta is None:
            # Legacy run — show its map but DON'T mutate the form fields.
            state["current_run"] = _legacy_run_record(run_id, results_dir)
        else:
            state["current_run"] = _run_record_from_meta(meta, run_id, results_dir)
            params = meta.get("params") or {}
            # Only overwrite keys we know about, in case meta.json contains
            # extras from a newer version or a stale schema.
            for k in DEFAULTS:
                if k in params:
                    state["form"][k] = str(params[k])
        return redirect(url_for("index"))

    @app.route("/rename/<run_id>", methods=["POST"])
    def rename_run(run_id):
        run_dir = (results_dir / run_id).resolve()
        try:
            run_dir.relative_to(results_dir)
        except ValueError:
            return Response("forbidden", status=403)
        if not run_dir.is_dir():
            return Response(f"run not found: {run_id}", status=404)
        body = request.get_json(silent=True) or {}
        name = body.get("name", "")
        if not isinstance(name, str):
            return Response("name must be a string", status=400)
        _write_name(run_dir, name)
        # If this is the active run, keep its display name in sync.
        if state["current_run"] and state["current_run"]["run_id"] == run_id:
            state["current_run"]["name"] = _normalize_name(name)
        return jsonify(ok=True, name=_normalize_name(name))

    @app.route("/blank_map.html")
    def blank_map():
        # Lazily generate (and cache to disk) a small empty Folium map for
        # the default right-pane view. We center on the default observer
        # location so the user lands somewhere reasonable for the Bay Area.
        # Files in results_dir starting with "_" are not real runs and are
        # ignored by _list_runs.
        blank_path = results_dir / "_blank_map.html"
        if not blank_path.is_file():
            try:
                center_lat = float(DEFAULTS["observer_lat"])
                center_lon = float(DEFAULTS["observer_lon"])
            except Exception:  # noqa: BLE001
                center_lat, center_lon = 37.5, -121.87
            write_blank_map(blank_path,
                            center_lat=center_lat, center_lon=center_lon)
        return send_from_directory(str(results_dir), "_blank_map.html")

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


class ActiveSearch:
    """Background search runner with cancellation + status reporting.

    Single-tenant (the UI doesn't expose parallel searches), so we keep all
    state in module-level instance. The /status endpoint reads `_status`
    under a lock; the /cancel endpoint flips the cancel event; the worker
    thread checks the event between lunar windows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._status: dict = {"running": False, "phase": "idle"}

    # ---- public surface ----------------------------------------------------

    def is_running(self) -> bool:
        with self._lock:
            return self._status.get("running", False)

    def status(self) -> dict:
        with self._lock:
            s = dict(self._status)
            # Recompute elapsed on every read so the timer ticks during
            # long single phases (e.g. mid-window numpy work).
            if s.get("started_at"):
                s["elapsed_s"] = time.time() - s["started_at"]
        return s

    def cancel(self) -> bool:
        if not self.is_running():
            return False
        self._cancel.set()
        self._update(phase="cancelling", message="stop requested")
        return True

    def start(self, form: dict, data_dir: Path, results_dir: Path) -> str:
        with self._lock:
            if self._status.get("running"):
                raise RuntimeError("a search is already running")
            self._cancel.clear()
            run_id = uuid4().hex
            self._status = {
                "running": True, "phase": "preparing", "run_id": run_id,
                "message": "", "windows_done": 0, "windows_total": None,
                "n_raw": 0, "started_at": time.time(), "elapsed_s": 0.0,
                "form": dict(form),
            }
        t = threading.Thread(
            target=self._run, args=(form, data_dir, results_dir, run_id),
            daemon=True, name=f"search-{run_id[:8]}",
        )
        with self._lock:
            self._thread = t
        t.start()
        return run_id

    # ---- internals ---------------------------------------------------------

    def _update(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)
            if "started_at" in self._status:
                self._status["elapsed_s"] = (
                    time.time() - self._status["started_at"])

    def _check_cancelled(self):
        if self._cancel.is_set():
            raise SearchCancelled()

    def _run(self, form: dict, data_dir: Path, results_dir: Path,
             run_id: str) -> None:
        early_cancel = False
        error: str | None = None
        try:
            self._do_search_impl(form, data_dir, results_dir, run_id)
        except SearchCancelled:
            early_cancel = True
            log.info("search %s cancelled before any results", run_id[:8])
        except Exception as e:  # noqa: BLE001
            error = repr(e)
            log.exception("background search %s failed", run_id[:8])
        finally:
            # Phase choice: error wins; otherwise if the cancel event was
            # ever set (early or mid-loop), it's "cancelled"; else "done".
            cancelled = early_cancel or self._cancel.is_set()
            phase = ("error" if error else
                     "cancelled" if cancelled else "done")
            msg = (error or
                   ("Stopped — partial results saved." if cancelled else
                    "Search complete."))
            self._update(running=False, phase=phase, message=msg)

    def _do_search_impl(self, form: dict, data_dir: Path, results_dir: Path,
                        run_id: str) -> None:
        out = results_dir / run_id
        out.mkdir(parents=True, exist_ok=True)

        bbox = tuple(float(x) for x in form["bbox"].split(","))
        assert len(bbox) == 4
        t0 = datetime.fromisoformat(form["start"]).replace(tzinfo=timezone.utc)
        t1 = datetime.fromisoformat(form["end"]).replace(tzinfo=timezone.utc)

        self._update(phase="loading_dem",
                     message="downloading/loading terrain…")
        self._check_cancelled()
        grid = load_terrain(bbox, float(form["dem_res"]),
                            cache_dir=data_dir / "dem_cache")

        self._update(phase="astronomy",
                     message="finding lunar windows…")
        self._check_cancelled()
        eng = AstroEngine(float(form["observer_lat"]),
                          float(form["observer_lon"]),
                          ephem_dir=data_dir)
        windows = eng.lunar_windows(
            t0, t1,
            alt_min_deg=float(form["alt_min"]),
            alt_max_deg=float(form["alt_max"]),
            sun_alt_max_deg=float(form["sun_alt_max"]),
            sun_alt_min_deg=float(form.get("sun_alt_min", -90)),
            phase_tolerance_deg=float(form["phase_tol"]),
            sample_step_minutes=float(form["sample_step_min"]),
        )
        self._update(windows_total=len(windows),
                     message=f"{len(windows)} lunar windows to scan")

        cfg = SearchConfig(
            d_min_m=float(form["d_min"]), d_max_m=float(form["d_max"]),
            alt_tol_deg=float(form["alt_tol"]),
            dedup_xy_snap_m=float(form["snap_m"]),
        )

        # ---- the long phase: scan windows incrementally ------------------
        # Cancellation inside this loop just BREAKS — we still want to
        # flow through dedup/filter/write so partial results land on disk.
        # Cancellation BEFORE the loop (above) raises SearchCancelled, in
        # which case there's nothing meaningful to save.
        self._update(phase="searching",
                     message=f"searching window 0/{len(windows)}…")
        raw: list[Candidate] = []
        search_t0 = time.time()
        for i, w, cands in iter_search_windows(grid, windows, cfg):
            raw.extend(cands)
            self._update(windows_done=i + 1, n_raw=len(raw),
                         message=(f"window {i+1}/{len(windows)} — "
                                  f"{len(raw):,} raw candidates so far"))
            if self._cancel.is_set():
                log.info("cancellation detected mid-search; finalizing %d "
                         "candidates from %d/%d windows",
                         len(raw), i + 1, len(windows))
                break
        search_elapsed = time.time() - search_t0

        # ---- the rest always runs (cancelled or not) so the user gets
        # the partial picture in CSV/map form.
        self._update(phase="dedup",
                     message=f"deduplicating {len(raw):,} candidates…")
        dedup = deduplicate(raw, cfg)

        self._update(phase="water_filter",
                     message="fetching water polygons (Overpass)…")
        try:
            water_idx = public_land.build_water_index_for_bbox(
                bbox, cache_dir=data_dir / "public_cache")
            dedup, n_water = public_land.filter_out_water(dedup, water_idx)
            if n_water:
                log.info("  water filter: dropped %d candidate(s)", n_water)
        except Exception:  # noqa: BLE001
            log.exception("water filter failed, continuing without it")

        self._update(phase="public_annotation",
                     message="fetching public-land polygons (Overpass)…")
        try:
            pl_index = public_land.build_index_for_bbox(
                bbox, cache_dir=data_dir / "public_cache")
            n_public = public_land.annotate(dedup, pl_index)
            log.info("  public-land annotation: %d / %d both-public",
                     n_public, len(dedup))
        except Exception:  # noqa: BLE001
            log.exception("public-land annotation failed; field will be None")

        self._update(phase="writing",
                     message=f"writing {len(dedup):,} candidates to disk…")
        pairs = cluster_by_pair(dedup, snap_m=float(form["snap_m"]))
        dedup.sort(key=lambda c: (-c.distance_m,
                                  abs(c.alt_actual_deg - c.alt_required_deg)))
        write_csv(dedup, out / "candidates.csv")
        write_geojson(dedup, out / "candidates.geojson")
        write_map(dedup, out / "map.html")

        summary = dict(
            n_raw=len(raw), n_unique=len(dedup), n_pairs=len(pairs),
            elapsed_s=search_elapsed,
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
            cancelled=self._cancel.is_set(),
        )
        meta = dict(version=1, params=form, summary=summary)
        (out / "meta.json").write_text(json.dumps(meta, indent=2))

        self._update(n_unique=len(dedup), n_pairs=len(pairs))


# Module-level singleton — only one background search at a time.
active_search = ActiveSearch()


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
