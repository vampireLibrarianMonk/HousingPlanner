from datetime import timezone
import requests
import streamlit as st

@st.cache_data(show_spinner=False)
def ors_directions_driving(
    api_key: str,
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
) -> tuple[float, float, list]:
    """
    Returns (distance_meters, duration_seconds) using ORS driving-car directions.
    Supports both ORS response formats and safely extracts metrics.
    """
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {
        "Content-Type": "application/json",
        "Authorization": api_key,
    }

    payload = {
        "coordinates": [
            [start_lon, start_lat],
            [end_lon, end_lat],
        ]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # ---------------------------------------
    # Parse ORS response (features or routes)
    # ---------------------------------------

    # GeoJSON-style response
    if "features" in data and data["features"]:
        props = data["features"][0].get("properties", {})
        summary = props.get("summary")
        segments = props.get("segments")

    # Classic routing response
    elif "routes" in data and data["routes"]:
        route = data["routes"][0]
        summary = route.get("summary")
        segments = route.get("segments")

    else:
        err_msg = data.get("error", {}).get("message", str(data))
        raise RuntimeError(f"OpenRouteService error: {err_msg}")

    # ---------------------------------------
    # Extract distance & duration safely
    # ---------------------------------------

    distance = summary.get("distance") if summary else None
    duration = summary.get("duration") if summary else None

    # Fallback: some ORS responses only populate segments
    if (distance is None or duration is None) and segments:
        distance = segments[0].get("distance")
        duration = segments[0].get("duration")

    if distance is None or duration is None:
        raise RuntimeError(
            "OpenRouteService response missing distance/duration "
            f"(summary={summary}, segments={segments})"
        )

    geometry = None

    if "features" in data:
        geometry = data["features"][0]["geometry"]["coordinates"]
    elif "routes" in data:
        geometry = data["routes"][0].get("geometry")

    return float(distance), float(duration), geometry


@st.cache_data(show_spinner=False)
def google_directions_driving(
    api_key: str,
    start: dict,
    end: dict,
    departure_dt,
) -> tuple[float, float, list]:
    """
    Returns (distance_meters, duration_seconds) using Google Routes API.
    Traffic-aware when departure_dt is provided.
    """
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "routes.duration,"
            "routes.distanceMeters,"
            "routes.polyline.encodedPolyline"
        ),
    }

    # ---------------------------------------
    # Ensure RFC3339 UTC timestamp (Z format)
    # ---------------------------------------
    if departure_dt.tzinfo is None:
        departure_dt = departure_dt.replace(tzinfo=timezone.utc)

    departure_ts = (
        departure_dt
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": start["lat"],
                    "longitude": start["lon"],
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": end["lat"],
                    "longitude": end["lon"],
                }
            }
        },
        "travelMode": "DRIVE",

        # Traffic-aware routing
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": departure_ts,

        # REQUIRED context
        "languageCode": "en-US",
        "units": "METRIC",

        # REQUIRED IN PRACTICE (even if all false)
        "routeModifiers": {
            "avoidTolls": False,
            "avoidHighways": False,
            "avoidFerries": False,
        },
    }

    # ---------------------------------------
    # DEBUG: inspect exact payload sent to Google
    # (TEMPORARY — remove after verification)
    # ---------------------------------------
    # st.code(payload, language="json")

    payload = {k: v for k, v in payload.items() if v is not None}

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    route = data["routes"][0]

    # ---------------------------------------
    # Distance & duration may exist at
    # route-level OR leg-level (Google quirk)
    # ---------------------------------------

    distance = route.get("distanceMeters")
    duration = route.get("duration")

    # Fallback to first leg if needed
    if (distance is None or duration is None) and route.get("legs"):
        leg = route["legs"][0]
        distance = leg.get("distanceMeters")
        duration = leg.get("duration")

    if distance is None or duration is None:
        raise RuntimeError(
            "Google Routes response missing distance/duration: "
            f"{route}"
        )

    polyline = route.get("polyline", {}).get("encodedPolyline")

    return (
        float(distance),
        float(duration.rstrip("s")),
        polyline,
    )