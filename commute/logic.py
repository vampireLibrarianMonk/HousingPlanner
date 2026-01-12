from datetime import datetime, timedelta, timezone

from .providers import (
    ors_directions_driving,
    google_directions_driving,
)
from .geometry import decode_geometry


def compute_commute(
    *,
    locations,
    home,
    stops_df,
    routing_method,
    ors_api_key,
    google_api_key,
    departure_time,
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
        else:
            dist_m, dur_s, geom = google_directions_driving(
                api_key=google_api_key,
                start=a,
                end=b,
                departure_dt=current_dt,
            )
            pts = decode_geometry(geom, "GOOGLE")
            provider = "Google"

        if all_route_points and pts:
            all_route_points.extend(pts[1:])
        else:
            all_route_points.extend(pts)

        segment_routes.append({
            "from": a["label"],
            "to": b["label"],
            "points": pts,
            "provider": provider,
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
    }
