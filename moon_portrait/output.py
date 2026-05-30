"""Map + CSV/GeoJSON output.

Renders the candidate list as:
  - a Folium / Leaflet HTML map with one marker per (camera, model) pair,
    a line connecting them, and a popup showing the candidate metadata
  - a CSV (one row per candidate)
  - a GeoJSON FeatureCollection (compatible with QGIS / Google Earth)

The map uses two base layers (OSM and ESRI World Imagery) so the user can
toggle between road map and satellite to judge openness / accessibility.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import timezone, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import folium
from folium import Element
from folium.features import DivIcon

from .search import Candidate


# Color tokens — keep these in sync with the legend HTML below.
CAMERA_COLOR = "#1f78b4"   # blue
MODEL_COLOR = "#e31a1c"    # red
LINE_COLOR = "#ffaa00"     # yellow-orange


LEGEND_HTML = """
<!-- moon-portrait-map-version: v7 (optimistic-delete) -->
<style>
  /* When this class is on the map root, hide all non-public pairs.
     Markers without a public class (no annotation) are never hidden,
     so the toggle becomes a no-op for candidates that lack the column. */
  .hide-non-public .pair-private { display: none !important; }

  /* Active-pair emphasis: when a popup is open, the JS in this map adds
     .has-active-pair on the map container and .active-pair on every
     element belonging to the clicked pair. CSS then dims everything else
     and slightly boldens the active pair's SVG strokes. Closing the
     popup clears both classes. */
  .has-active-pair .pair-elt {
    opacity: 0.30 !important;
    transition: opacity 120ms ease-out;
  }
  .has-active-pair .pair-elt.active-pair {
    opacity: 1.0 !important;
  }
  .has-active-pair .leaflet-overlay-pane path.pair-elt.active-pair {
    stroke-width: 4 !important;
  }
  .has-active-pair .moon-arrow.pair-elt.active-pair > div {
    filter: drop-shadow(0 0 2px rgba(0,0,0,0.5));
  }
  .pair-public-toggle { margin-top: 6px; padding-top: 6px;
                        border-top: 1px solid #ddd; font-size: 11px;
                        color: #444; }
  .pair-public-toggle input { margin-right: 4px; vertical-align: -1px; }
  .pair-public-toggle .hint { color: #888; font-style: italic; }
</style>
<div style="position: fixed; bottom: 18px; left: 18px; z-index: 1000;
            background: rgba(255,255,255,0.92); padding: 8px 12px;
            border: 1px solid #888; border-radius: 4px; max-width: 320px;
            font: 12px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2); line-height: 1.55;">
  <div style="font-weight: 600; margin-bottom: 4px;">Legend</div>
  <div><span style="color: %s; font-size: 16px; vertical-align: -1px;">●</span>
       &nbsp;Camera (photographer)</div>
  <div><span style="color: %s; font-size: 16px; vertical-align: -1px;">●</span>
       &nbsp;Model (subject)</div>
  <div style="display:flex; align-items:center; gap:4px;">
    <svg width="44" height="10" viewBox="0 0 44 10">
      <line x1="0" y1="5" x2="22" y2="5" stroke="%s" stroke-width="2"/>
      <line x1="22" y1="5" x2="36" y2="5" stroke="%s" stroke-width="2"
            stroke-dasharray="3,2"/>
      <polygon points="36,1 44,5 36,9" fill="%s"/>
    </svg>
    &nbsp;Sight line; arrow points toward the Moon
  </div>
  <div class="pair-public-toggle">
    <label>
      <input type="checkbox"
             onchange="document.documentElement.classList.toggle(
                       'hide-non-public', this.checked)">
      Show only likely-public pairs
    </label>
    <div class="hint">
      Filters out pairs where either point lies outside an OSM
      park / protected area. Heuristic — verify before traveling.
    </div>
  </div>
</div>
""" % (CAMERA_COLOR, MODEL_COLOR, LINE_COLOR, LINE_COLOR, LINE_COLOR)


# Pacific Time (used only for display; canonical timestamps stay UTC).
PT_OFFSET_STD = timedelta(hours=-8)   # PST
PT_OFFSET_DST = timedelta(hours=-7)   # PDT


def _pt_str(t_utc) -> str:
    """Format UTC datetime as Pacific local time string with PT label.

    Crude DST rule (US: 2nd Sun Mar to 1st Sun Nov) — fine for our use; the
    canonical UTC time is also shown in popups.
    """
    # Simple heuristic — Pacific is on DST mid-March through early November.
    # Good enough for labeling; if the date lands on a DST boundary weekend
    # the user should consult the UTC time.
    if 3 <= t_utc.month <= 10:
        offset = PT_OFFSET_DST
        label = "PDT"
    elif t_utc.month == 11 and t_utc.day <= 6:
        offset = PT_OFFSET_DST
        label = "PDT"
    elif t_utc.month == 3 and t_utc.day >= 8:
        offset = PT_OFFSET_DST
        label = "PDT"
    else:
        offset = PT_OFFSET_STD
        label = "PST"
    local = (t_utc + offset).replace(tzinfo=None)
    return f"{local:%Y-%m-%d %H:%M} {label}"


def write_csv(cands: Sequence[Candidate], path: Path | str) -> None:
    path = Path(path)
    if not cands:
        path.write_text("")
        return
    cols = list(cands[0].as_dict().keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in cands:
            row = c.as_dict()
            row["time_utc"] = c.time_utc.isoformat()
            w.writerow(row)


def write_geojson(cands: Sequence[Candidate], path: Path | str) -> None:
    features = []
    for i, c in enumerate(cands):
        # Line from camera to model
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[c.camera_lon, c.camera_lat],
                                [c.model_lon, c.model_lat]],
            },
            "properties": {
                "id": i, "kind": "sight_line",
                "time_utc": c.time_utc.isoformat(),
                "distance_m": c.distance_m,
                "az_deg": c.az_required_deg,
                "alt_deg": c.alt_required_deg,
                "alt_actual_deg": c.alt_actual_deg,
                "elev_gain_m": c.elev_gain_m,
                "moon_phase_deg": c.moon_phase_deg,
                "sun_alt_deg": c.sun_alt_deg,
                "camera_public": c.camera_public,
                "model_public": c.model_public,
            },
        })
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [c.camera_lon, c.camera_lat]},
            "properties": {"id": i, "kind": "camera",
                           "elev_m": c.camera_elev_m,
                           "public": c.camera_public},
        })
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [c.model_lon, c.model_lat]},
            "properties": {"id": i, "kind": "model",
                           "elev_m": c.model_elev_m,
                           "public": c.model_public},
        })
    Path(path).write_text(json.dumps({"type": "FeatureCollection",
                                       "features": features}, indent=1))


def write_blank_map(
    path: Path | str,
    center_lat: float = 37.5,
    center_lon: float = -121.87,
    zoom: int = 10,
) -> None:
    """Render an empty Leaflet map with the same base layers as a results map.

    Used by the web UI as the default view before any run is loaded — gives
    the user a real map to pan/zoom against so they can click "use map view"
    next to the BBox field without having to first run a search.
    """
    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom,
                   tiles=None)
    # See write_map() for the rationale on show=True/False here.
    folium.TileLayer(
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        name="OpenStreetMap",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        max_zoom=19, show=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Satellite (Esri)", attr="Tiles &copy; Esri",
        max_zoom=19, show=False,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        name="Topo (Esri)", attr="Tiles &copy; Esri",
        max_zoom=19, show=False,
    ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    hint_html = (
        '<div style="position: fixed; top: 14px; left: 50%; '
        'transform: translateX(-50%); z-index: 1000; '
        'background: rgba(255,255,255,0.94); padding: 8px 14px; '
        'border: 1px solid #888; border-radius: 4px; '
        'font: 13px -apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif; '
        'box-shadow: 0 2px 4px rgba(0,0,0,0.2);">'
        'Pan and zoom to your area of interest, then click '
        '<b>use map view</b> next to BBox and <b>Search</b>.'
        '</div>'
    )
    m.get_root().html.add_child(Element(hint_html))
    m.save(str(path))


def cluster_by_pair(
    cands: Sequence[Candidate], snap_m: float = 75.0
) -> list[list[Candidate]]:
    """Group candidates into clusters that share the same (camera, model) pair.

    Two candidates are in the same group iff their camera and model UTM
    coordinates both fall in the same `snap_m`-sized bin. Within a group,
    all candidates are the same physical setup at different times (different
    moon transits / full moons across the year).
    """
    groups: dict[tuple, list[Candidate]] = {}
    for c in cands:
        key = (round(c.camera_x / snap_m), round(c.camera_y / snap_m),
               round(c.model_x / snap_m), round(c.model_y / snap_m))
        groups.setdefault(key, []).append(c)
    return list(groups.values())


def write_map(cands: Sequence[Candidate], path: Path | str,
              center_lat: float | None = None,
              center_lon: float | None = None,
              cluster_snap_m: float = 75.0,
              max_pairs: int = 500) -> None:
    """Render candidates as an interactive Folium map.

    Candidates are first clustered into (camera, model) pairs (since the same
    physical setup often appears across multiple lunar windows). Each cluster
    is one marker pair on the map; its popup lists every time the moon aligns.

    Up to `max_pairs` clusters are rendered (largest distance first, then
    most timing opportunities), to keep the HTML map responsive.
    """
    pairs = cluster_by_pair(list(cands), snap_m=cluster_snap_m)
    if not pairs:
        # Show a banner instead of a silent empty map.
        m = folium.Map(location=[37.39, -122.08], zoom_start=9)
        banner = (
            '<div style="position:absolute;top:12px;left:50%;'
            'transform:translateX(-50%);z-index:10000;'
            'background:#ffeecc;border:1px solid #cc8800;'
            'padding:10px 16px;font:14px sans-serif;border-radius:4px">'
            'No candidates found for the chosen constraints.<br>'
            '<small>Try widening the date range, altitude band, distance range, '
            'or alt-match tolerance; or check that the bbox covers terrain '
            'with the required elevation gain.</small></div>'
        )
        m.get_root().html.add_child(Element(banner))
        m.save(str(path))
        return

    # Score each pair: prefer longer distance, then more opportunities, then
    # better alt match for the best opportunity.
    def pair_score(pair):
        best_d = max(c.distance_m for c in pair)
        best_alt = min(abs(c.alt_actual_deg - c.alt_required_deg) for c in pair)
        return (-best_d, -len(pair), best_alt)
    pairs.sort(key=pair_score)
    pairs = pairs[:max_pairs]

    if center_lat is None:
        center_lat = sum(c.camera_lat for p in pairs for c in p) / sum(len(p) for p in pairs)
    if center_lon is None:
        center_lon = sum(c.camera_lon for p in pairs for c in p) / sum(len(p) for p in pairs)

    m = folium.Map(location=[center_lat, center_lon], zoom_start=12, tiles=None)
    # Only OSM is shown initially (show=True). The other base layers are
    # registered with show=False so they're available to switch to via the
    # layer control but don't all start stacked on the map. Without this,
    # all three render simultaneously initially (60 tiles fetched at once),
    # Topo wins the z-order, and the layer control then performs add/remove
    # in a confusing order that breaks the OSM switch path in some browsers.
    folium.TileLayer(
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        name="OpenStreetMap",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        max_zoom=19, show=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Satellite (Esri)",
        attr="Tiles &copy; Esri",
        max_zoom=19, show=False,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        name="Topo (Esri)",
        attr="Tiles &copy; Esri",
        max_zoom=19, show=False,
    ).add_to(m)

    cam_layer = folium.FeatureGroup(
        name=f'<span style="color:{CAMERA_COLOR}">●</span> Cameras',
        show=True).add_to(m)
    mod_layer = folium.FeatureGroup(
        name=f'<span style="color:{MODEL_COLOR}">●</span> Models',
        show=True).add_to(m)
    line_layer = folium.FeatureGroup(
        name=f'<span style="color:{LINE_COLOR}">━▸</span> Sight lines',
        show=True).add_to(m)

    # Pair metadata for the post-render JS tagger. Folium's `className`
    # kwarg on CircleMarker / PolyLine is not consistently honored across
    # versions, so instead of relying on it we tag the rendered SVG paths
    # and DivIcons from JavaScript once Leaflet has finished rendering.
    pair_js_data: list[dict] = []

    for i, pair in enumerate(pairs):
        # Representative: the candidate within this pair with the best
        # alt-match.
        rep = min(pair, key=lambda c: abs(c.alt_actual_deg - c.alt_required_deg))

        # Public-land status for the pair. Three states:
        #   "pair-public":  both points in a public-access polygon
        #   "pair-private": at least one outside (or annotation says False)
        #   "":             not annotated (None) — toggle won't affect it
        cam_pub = rep.camera_public
        mod_pub = rep.model_public
        if cam_pub is None and mod_pub is None:
            public_class = ""        # toggle does nothing for this pair
            public_label = ""
        elif cam_pub and mod_pub:
            public_class = "pair-public"
            public_label = ("<br><span style='color:#2a8'>✓</span> "
                            "Both points likely on public land (OSM)")
        else:
            public_class = "pair-private"
            camp_t = "in park" if cam_pub else "outside park"
            modp_t = "in park" if mod_pub else "outside park"
            public_label = (f"<br><span style='color:#c33'>!</span> "
                            f"camera {camp_t}; model {modp_t} (OSM)")

        # Build a table of all timing opportunities for this pair.
        pair_sorted = sorted(pair, key=lambda c: c.time_utc)
        rows = ""
        for c in pair_sorted:
            rows += (
                f"<tr><td style='padding:2px 6px'>{_pt_str(c.time_utc)}</td>"
                f"<td style='padding:2px 6px'>{c.time_utc:%H:%M}Z</td>"
                f"<td style='padding:2px 6px'>{c.distance_m:.0f}</td>"
                f"<td style='padding:2px 6px'>{c.az_required_deg:.1f}</td>"
                f"<td style='padding:2px 6px'>"
                f"{c.alt_required_deg:.2f} / {c.alt_actual_deg:.2f}</td>"
                f"<td style='padding:2px 6px'>{c.moon_phase_deg:.1f}°</td>"
                f"<td style='padding:2px 6px'>{c.sun_alt_deg:.1f}°</td></tr>"
            )

        popup_html = (
            f"<b>Pair #{i+1}</b> &mdash; {len(pair)} timing(s)<br>"
            f"distance: {rep.distance_m:.0f} m, "
            f"elev gain: {rep.elev_gain_m:+.1f} m<br>"
            f"camera: {rep.camera_lat:.5f}, {rep.camera_lon:.5f} "
            f"({rep.camera_elev_m:.0f} m)<br>"
            f"model:  {rep.model_lat:.5f}, {rep.model_lon:.5f} "
            f"({rep.model_elev_m:.0f} m)<br>"
            f"compass: {_compass(rep.az_required_deg)}"
            f"{public_label}<br><br>"
            "<table style='border-collapse:collapse;font-size:11px'>"
            "<tr style='background:#eee;font-weight:bold'>"
            "<td style='padding:2px 6px'>Local time</td>"
            "<td style='padding:2px 6px'>UTC</td>"
            "<td style='padding:2px 6px'>d (m)</td>"
            "<td style='padding:2px 6px'>az °</td>"
            "<td style='padding:2px 6px'>alt req/act °</td>"
            "<td style='padding:2px 6px'>phase</td>"
            "<td style='padding:2px 6px'>sun</td></tr>"
            f"{rows}"
            "</table><br>"
            f"<a href='https://www.google.com/maps/?q={rep.camera_lat},{rep.camera_lon}' "
            f"target='_blank'>📍 camera in Google Maps</a> &middot; "
            f"<a href='https://www.google.com/maps/?q={rep.model_lat},{rep.model_lon}' "
            f"target='_blank'>📍 model in Google Maps</a><br>"
            f"<a href='https://earth.google.com/web/@{rep.camera_lat},{rep.camera_lon},"
            f"{rep.camera_elev_m+1.5}a,500d,30y,{rep.az_required_deg}h,"
            f"{90-rep.alt_required_deg}t,0r' target='_blank'>🌐 camera POV in Google Earth</a>"
            f"<br><br>"
            f"<button onclick='window.moonDeletePair && window.moonDeletePair("
            f"{i},"  # pair index — for instant DOM hide
            f"{rep.camera_x:.3f},{rep.camera_y:.3f},"
            f"{rep.model_x:.3f},{rep.model_y:.3f})' "
            f"style='padding:4px 10px;background:#fff;border:1px solid #c33;"
            f"color:#c33;cursor:pointer;font-size:11px;border-radius:3px;'>"
            f"🗑 Delete this pair</button>"
        )
        tooltip = (f"Pair #{i+1} — d={rep.distance_m:.0f}m, "
                   f"alt={rep.alt_required_deg:.1f}°, "
                   f"{len(pair)} timing(s)")

        # Extension past the model in the Moon direction. Same horizontal
        # bearing as camera→model (by construction the bearing equals the
        # Moon's azimuth at the candidate time). We extend by 25% of the
        # camera-model distance, capped at 150 m, in WGS84 lat/lon — fine
        # for tiny offsets at mid-latitudes.
        az_rad = math.radians(rep.az_required_deg)
        ext_m = min(150.0, rep.distance_m * 0.25)
        dlat = (ext_m * math.cos(az_rad)) / 111_320.0
        dlon = (ext_m * math.sin(az_rad)) / (
            111_320.0 * max(0.01, math.cos(math.radians(rep.model_lat))))
        tip_lat = rep.model_lat + dlat
        tip_lon = rep.model_lon + dlon

        # NOTE: folium.Popup is a stateful child object and cannot be
        # attached to multiple shapes — doing so produces an "undefined
        # bindPopup" JS error at load and silently kills every marker after
        # the first attachment. Build a fresh Popup per shape.
        folium.CircleMarker(
            location=[rep.camera_lat, rep.camera_lon], radius=6,
            color=CAMERA_COLOR, fill=True, fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=680), tooltip=tooltip,
        ).add_to(cam_layer)
        folium.CircleMarker(
            location=[rep.model_lat, rep.model_lon], radius=6,
            color=MODEL_COLOR, fill=True, fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=680), tooltip=tooltip,
        ).add_to(mod_layer)
        # Solid segment: camera → model (the actual line of sight).
        folium.PolyLine(
            locations=[[rep.camera_lat, rep.camera_lon],
                       [rep.model_lat, rep.model_lon]],
            color=LINE_COLOR, weight=2, opacity=0.85,
            popup=folium.Popup(popup_html, max_width=680),
        ).add_to(line_layer)
        # Dashed segment: model → tip, indicating the continuation toward the
        # Moon (this is the path the Moon's light travels along to reach
        # the camera's eye, grazing the model's head).
        folium.PolyLine(
            locations=[[rep.model_lat, rep.model_lon],
                       [tip_lat, tip_lon]],
            color=LINE_COLOR, weight=2, opacity=0.85,
            dash_array="4,3",
        ).add_to(line_layer)
        # Arrowhead at the tip, rotated by the Moon's azimuth. The SVG is
        # drawn pointing up (north = 0°), so a CSS rotate(az°) — clockwise
        # from 12 o'clock — matches the bearing convention exactly.
        arrow_html = (
            f'<div style="transform: rotate({rep.az_required_deg}deg); '
            f'transform-origin: 7px 7px; width: 14px; height: 14px;">'
            f'<svg width="14" height="14" viewBox="0 0 14 14">'
            f'<polygon points="7,0 13,12 1,12" fill="{LINE_COLOR}" '
            f'stroke="#996600" stroke-width="0.5"/>'
            f'</svg></div>'
        )
        folium.Marker(
            location=[tip_lat, tip_lon],
            icon=DivIcon(html=arrow_html, icon_size=(14, 14),
                         icon_anchor=(7, 7), class_name="moon-arrow"),
        ).add_to(line_layer)

        # Stash pair geometry + intended public class for the JS tagger
        # to find by lat/lon. ALWAYS emit (even when public_class is "")
        # so every pair gets the pair-elt + pair-idx-N classes used by the
        # popup-emphasis effect; the public_class is added only when set.
        pair_js_data.append({
            "idx": i,
            "cam": [rep.camera_lat, rep.camera_lon],
            "mod": [rep.model_lat, rep.model_lon],
            "tip": [tip_lat, tip_lon],
            "cls": public_class,  # may be ""
        })

    # collapsed=False so the toggle list is open by default, and we set
    # autoZIndex so feature-group labels can render the inline-colored swatches.
    folium.LayerControl(collapsed=False).add_to(m)

    # On-map legend overlay (visible regardless of layer-control state).
    m.get_root().html.add_child(Element(LEGEND_HTML))

    # Post-render JS that tags rendered Leaflet elements with the correct
    # pair-public / pair-private class. This is more robust than relying on
    # Folium to pass `className` through CircleMarker/PolyLine, which is
    # version-dependent in practice.
    tagger_data_json = json.dumps(pair_js_data)
    snap_m_js = float(cluster_snap_m)
    tagger_js = f"""
<script>
(function() {{
  const PAIRS = {tagger_data_json};
  const SNAP_M = {snap_m_js};
  // Derive run_id from the URL we were served at:
  // /download/<run_id>/map.html  -> run_id = group(1)
  const _m = window.location.pathname.match(/\\/download\\/([^/]+)\\/map\\.html/);
  const RUN_ID = _m ? _m[1] : null;

  // Optimistic delete: hide the pair's elements right away, close the
  // popup, then fire the server-side delete in the background. The page
  // is NOT reloaded — the user can keep clicking other markers while
  // requests queue up server-side (serialized per-run by a backend lock).
  // pairIdx is the integer that matches the .pair-idx-<N> class the
  // tagger applied to every element of this pair.
  function hidePairLocally(pairIdx) {{
    document.querySelectorAll('.pair-idx-' + pairIdx).forEach(el => {{
      el.style.display = 'none';
      el.classList.add('pair-deleted-locally');
    }});
  }}
  function restorePairLocally(pairIdx) {{
    document.querySelectorAll('.pair-idx-' + pairIdx).forEach(el => {{
      el.style.display = '';
      el.classList.remove('pair-deleted-locally');
    }});
  }}
  function findMapInstance() {{
    for (const k in window) {{
      if (k.indexOf('map_') === 0 && window[k]
          && typeof window[k].closePopup === 'function') return window[k];
    }}
    return null;
  }}
  window.moonDeletePair = function(pairIdx, camX, camY, modX, modY) {{
    if (!RUN_ID) {{
      alert("Can't delete: this map isn't being served by the moon-portrait web UI.");
      return;
    }}
    if (!confirm("Permanently delete this pair from this run? " +
                 "candidates.csv, candidates.geojson, and map.html will be rewritten."))
      return;

    // 1) Instant feedback: hide the pair and close the popup right now.
    hidePairLocally(pairIdx);
    const m = findMapInstance();
    if (m) m.closePopup();

    // 2) Fire the server delete without awaiting. Multiple in-flight
    //    requests are fine — the backend serializes them per run id.
    fetch("/delete_pair/" + RUN_ID, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{cam_x: camX, cam_y: camY,
                             mod_x: modX, mod_y: modY,
                             snap_m: SNAP_M}}),
    }}).then(async r => {{
      if (!r.ok) {{
        // Server rejected — undo the local hide so the user can see it.
        restorePairLocally(pairIdx);
        const txt = await r.text();
        console.warn("delete_pair failed:", r.status, txt);
        return;
      }}
      // Quietly refresh the parent's sidebar pair count.
      try {{ if (window.parent && window.parent.refreshRunsList)
              window.parent.refreshRunsList(); }} catch (e) {{}}
    }}).catch(e => {{
      restorePairLocally(pairIdx);
      console.warn("delete_pair network error:", e);
    }});
  }};

  if (!PAIRS.length) return;
  const EPS = 1e-6;
  function near(a, b) {{
    return Math.abs(a[0] - b[0]) < EPS && Math.abs(a[1] - b[1]) < EPS;
  }}
  function addClasses(el, pr) {{
    if (!el) return;
    el.classList.add('pair-elt');
    el.classList.add('pair-idx-' + pr.idx);
    if (pr.cls) el.classList.add(pr.cls);
  }}
  function findMap() {{
    for (const k in window) {{
      if (k.indexOf('map_') === 0 && window[k]
          && typeof window[k].eachLayer === 'function') return window[k];
    }}
    return null;
  }}
  function tagAll() {{
    const map = findMap();
    if (!map || typeof L === 'undefined') {{ return setTimeout(tagAll, 100); }}
    let tagged = 0;
    map.eachLayer(function(layer) {{
      // CircleMarker (camera / model dots): match by single lat/lon.
      if (layer instanceof L.CircleMarker && typeof layer.getLatLng === 'function'
          && !(layer instanceof L.Marker)) {{
        const ll = layer.getLatLng();
        const p = [ll.lat, ll.lng];
        for (const pr of PAIRS) {{
          if (near(p, pr.cam) || near(p, pr.mod)) {{
            addClasses(layer._path, pr); tagged++; break;
          }}
        }}
      }}
      // Polyline (solid and dashed segments): match by both endpoints.
      if (layer instanceof L.Polyline && !(layer instanceof L.Polygon)
          && !(layer instanceof L.CircleMarker)) {{
        const lls = layer.getLatLngs();
        if (lls.length >= 2) {{
          const a = [lls[0].lat, lls[0].lng];
          const b = [lls[1].lat, lls[1].lng];
          for (const pr of PAIRS) {{
            if ((near(a, pr.cam) && near(b, pr.mod)) ||
                (near(a, pr.mod) && near(b, pr.tip))) {{
              addClasses(layer._path, pr); tagged++; break;
            }}
          }}
        }}
      }}
      // Marker (the arrow at the tip): match by tip lat/lon.
      if (layer instanceof L.Marker && typeof layer.getLatLng === 'function') {{
        const ll = layer.getLatLng();
        const p = [ll.lat, ll.lng];
        for (const pr of PAIRS) {{
          if (near(p, pr.tip)) {{ addClasses(layer._icon, pr); tagged++; break; }}
        }}
      }}
    }});
    window.__pairTagsApplied = tagged;

    // ---- popup-driven emphasis -------------------------------------
    // When any popup opens, find which pair it belongs to (by scanning
    // up to "Pair #N" in the popup HTML) and add .active-pair to that
    // pair's elements + .has-active-pair on the map container. Closing
    // the popup clears them.
    const container = map.getContainer();
    function clearActive() {{
      container.classList.remove('has-active-pair');
      document.querySelectorAll('.active-pair').forEach(el => {{
        el.classList.remove('active-pair');
      }});
    }}
    map.on('popupopen', function(e) {{
      let idx = null;
      const src = e.popup && e.popup._source;
      // Preferred: read the index off the source layer's tagged classes.
      const cls = (src && (src._path || src._icon))
                && (src._path || src._icon).classList;
      if (cls) {{
        for (const c of cls) {{
          if (c.indexOf('pair-idx-') === 0) {{
            idx = c.substring('pair-idx-'.length); break;
          }}
        }}
      }}
      // Fallback: parse "Pair #N" out of the rendered popup text.
      if (idx === null && e.popup && e.popup._contentNode) {{
        const m = e.popup._contentNode.innerText.match(/Pair #(\\d+)/);
        if (m) idx = String(parseInt(m[1], 10) - 1);
      }}
      if (idx === null) return;
      clearActive();
      container.classList.add('has-active-pair');
      document.querySelectorAll('.pair-idx-' + idx).forEach(el => {{
        el.classList.add('active-pair');
      }});
    }});
    map.on('popupclose', clearActive);
  }}
  if (document.readyState === 'complete') tagAll();
  else window.addEventListener('load', tagAll);
}})();
</script>
"""
    m.get_root().html.add_child(Element(tagger_js))

    # Auto-fit map to encompass every candidate point. Without this, an
    # observer-centered start view may not include the actual pair locations.
    all_lats = [c.camera_lat for p in pairs for c in p] + \
               [c.model_lat  for p in pairs for c in p]
    all_lons = [c.camera_lon for p in pairs for c in p] + \
               [c.model_lon  for p in pairs for c in p]
    if all_lats and all_lons:
        m.fit_bounds([[min(all_lats), min(all_lons)],
                      [max(all_lats), max(all_lons)]],
                     padding=(30, 30))
    m.save(str(path))


def _compass(az_deg: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = int((az_deg + 11.25) // 22.5) % 16
    return pts[idx]
