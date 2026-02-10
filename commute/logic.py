from datetime import datetime, timedelta, timezone
import time

from .providers import (
    ors_directions_driving,
    google_directions_driving,
    waze_directions_driving,
)
from .geometry import decode_geometry
from .infra_providers import fetch_waze_incidents
from .infra_normalize import normalize_waze_alerts, normalize_waze_jams
from .infra_spatial import build_route_buffer, filter_events_by_buffer


def compute_commute(
    *,
    locations,
    home,
    stops_df,
    routing_method,
    ors_api_key,
    google_api_key,
    waze_api_key,
    departure_time,
    status_callback=None,
):
    """
    Pure commute computation.

    Returns:
        dict with keys:
          - segments_df
          - total_m
          - total_s
          - segment_routes
    """

    seg_rows = []
    total_m = 0.0
    total_s = 0.0

    all_route_points = []
    segment_routes = []

    # Resolve ordered + revisit locations
    ordered_locs = []
    revisit_locs = []
    missing = []

    for _, row in stops_df.iterrows():
        label = row["Label"]
        loc = next((l for l in locations if l["label"] == label), None)
        if not loc:
            missing.append(label)
            continue

        ordered_locs.append(loc)

        if row.get("Revisit", False):
            revisit_locs.append(loc)

    if missing:
        raise ValueError(
            "Missing locations: " + ", ".join(missing)
        )

    points = [home] + ordered_locs + revisit_locs + [home]
    return_start_index = len(ordered_locs)

    # Initialize clock (Google requires >= now)
    now = datetime.now(tz=timezone.utc)
    candidate_dt = now.replace(
        hour=departure_time.hour,
        minute=departure_time.minute,
        second=0,
        microsecond=0,
    )

    if candidate_dt <= now:
        candidate_dt = now + timedelta(minutes=1)

    current_dt = candidate_dt

    for i in range(len(points) - 1):
        a = points[i]
        b = points[i + 1]

        if routing_method.startswith("OpenRouteService"):
            dist_m, dur_s, geom = ors_directions_driving(
                api_key=ors_api_key,
                start_lon=a["lon"], start_lat=a["lat"],
                end_lon=b["lon"], end_lat=b["lat"],
            )
            pts = decode_geometry(geom, "ORS")
            provider = "ORS"
        elif routing_method.startswith("Google"):
            dist_m, dur_s, geom = google_directions_driving(
                api_key=google_api_key,
                start=a,
                end=b,
                departure_dt=current_dt,
            )
            pts = decode_geometry(geom, "GOOGLE")
            provider = "Google"
        else:
            attempt = 0
            backoffs = [1, 2]
            last_exc = None
            while True:
                try:
                    if status_callback:
                        status_callback(
                            f"Waze routing {a['label']} → {b['label']} (attempt {attempt + 1})"
                        )
                    dist_m, dur_s, geom = waze_directions_driving(
                        api_key=waze_api_key,
                        start=a,
                        end=b,
                        departure_timestamp=int(current_dt.timestamp()),
                        arrival_timestamp=None,
                    )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt >= len(backoffs):
                        break
                    if status_callback:
                        status_callback(
                            f"Waze timeout, retrying in {backoffs[attempt]}s..."
                        )
                    time.sleep(backoffs[attempt])
                    attempt += 1
            if last_exc:
                raise last_exc

            pts = decode_geometry(geom, "WAZE") if geom else []
            if not pts:
                try:
                    _, _, ors_geom = ors_directions_driving(
                        api_key=ors_api_key,
                        start_lon=a["lon"],
                        start_lat=a["lat"],
                        end_lon=b["lon"],
                        end_lat=b["lat"],
                    )
                    pts = decode_geometry(ors_geom, "ORS")
                except Exception:
                    pts = []
            provider = "Waze"

        if all_route_points and pts:
            all_route_points.extend(pts[1:])
        else:
            all_route_points.extend(pts)

        segment_routes.append({
            "leg_index": i,
            "from": a["label"],
            "to": b["label"],
            "label": f"{a['label']} → {b['label']}",
            "points": pts,
            "provider": provider,
            "is_return_leg": i >= return_start_index,
        })

        arrive_dt = current_dt + timedelta(seconds=dur_s)

        loiter_min = 0
        match = stops_df[stops_df["Label"] == b["label"]]
        if not match.empty:
            loiter_min = int(match.iloc[0].get("Loiter (min)", 0))

        leave_dt = arrive_dt + timedelta(minutes=loiter_min)

        total_m += dist_m
        total_s += dur_s + (loiter_min * 60)

        seg_rows.append({
            "From": a["label"],
            "To": b["label"],
            "Depart": current_dt.strftime("%H:%M"),
            "Arrive": arrive_dt.strftime("%H:%M"),
            "Drive (min)": round(dur_s / 60.0, 1),
            "Loiter (min)": loiter_min,
            "Leave": leave_dt.strftime("%H:%M"),
            "Cumulative (min)": round(total_s / 60.0, 1),
        })

        current_dt = leave_dt

    return {
        "segments": seg_rows,
        "total_m": total_m,
        "total_s": total_s,
        "segment_routes": segment_routes,
        "route_points": all_route_points,
    }


def compute_infrastructure_support(
    *,
    route_points,
    waze_api_key,
    buffer_m=200.0,
):
    if not route_points:
        return {
            "events": [],
            "summary": {},
        }

    lats = [pt[0] for pt in route_points]
    lons = [pt[1] for pt in route_points]
    bbox = (min(lons), min(lats), max(lons), max(lats))

    waze_raw = fetch_waze_incidents(waze_api_key, bbox) or {}
    waze_alerts = normalize_waze_alerts(waze_raw.get("alerts", []) or [])
    waze_jams = normalize_waze_jams(waze_raw.get("jams", []) or [])
    waze_events = waze_alerts + waze_jams

    buffer_geom = build_route_buffer(route_points, meters=buffer_m)
    events = filter_events_by_buffer(waze_events, buffer_geom)

    summary = {
        "incidents": sum(1 for e in events if e.get("event_type") == "incident"),
        "jams": sum(1 for e in events if e.get("event_type") == "jam"),
        "total": len(events),
    }

    return {
        "events": events,
        "summary": summary,
        "buffer_bbox": bbox,
    }
