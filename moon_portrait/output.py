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
from datetime import timezone, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import folium
from folium.features import DivIcon

from .search import Candidate


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
            },
        })
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [c.camera_lon, c.camera_lat]},
            "properties": {"id": i, "kind": "camera",
                           "elev_m": c.camera_elev_m},
        })
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [c.model_lon, c.model_lat]},
            "properties": {"id": i, "kind": "model",
                           "elev_m": c.model_elev_m},
        })
    Path(path).write_text(json.dumps({"type": "FeatureCollection",
                                       "features": features}, indent=1))


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
        from folium import Element
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
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Satellite (Esri)",
        attr="Tiles &copy; Esri",
    ).add_to(m)
    folium.TileLayer(
        tiles="https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        name="Topo (Esri)",
        attr="Tiles &copy; Esri",
    ).add_to(m)

    cam_layer = folium.FeatureGroup(name="Cameras", show=True).add_to(m)
    mod_layer = folium.FeatureGroup(name="Models", show=True).add_to(m)
    line_layer = folium.FeatureGroup(name="Sight lines", show=True).add_to(m)

    for i, pair in enumerate(pairs):
        # Representative: the candidate within this pair with the best
        # alt-match.
        rep = min(pair, key=lambda c: abs(c.alt_actual_deg - c.alt_required_deg))

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
            f"compass: {_compass(rep.az_required_deg)}<br><br>"
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
        )
        tooltip = (f"Pair #{i+1} — d={rep.distance_m:.0f}m, "
                   f"alt={rep.alt_required_deg:.1f}°, "
                   f"{len(pair)} timing(s)")

        # NOTE: folium.Popup is a stateful child object and cannot be
        # attached to multiple shapes — doing so produces an "undefined
        # bindPopup" JS error at load and silently kills every marker after
        # the first attachment. Build a fresh Popup per shape.
        folium.CircleMarker(
            location=[rep.camera_lat, rep.camera_lon], radius=6,
            color="#1f78b4", fill=True, fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=680), tooltip=tooltip,
        ).add_to(cam_layer)
        folium.CircleMarker(
            location=[rep.model_lat, rep.model_lon], radius=6,
            color="#e31a1c", fill=True, fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=680), tooltip=tooltip,
        ).add_to(mod_layer)
        folium.PolyLine(
            locations=[[rep.camera_lat, rep.camera_lon],
                       [rep.model_lat, rep.model_lon]],
            color="#ffaa00", weight=2, opacity=0.7,
            popup=folium.Popup(popup_html, max_width=680),
        ).add_to(line_layer)

    folium.LayerControl().add_to(m)

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
