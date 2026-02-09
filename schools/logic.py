from __future__ import annotations

import difflib
import math
import re
from typing import Iterable
from urllib.parse import quote_plus

from shapely.geometry import Point, shape
from shapely.ops import transform
import pyproj


# State abbreviation to full name mapping for GreatSchools URLs
STATE_FULL_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "DC": "district-of-columbia",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia",
    "WA": "washington", "WV": "west-virginia", "WI": "wisconsin", "WY": "wyoming",
}


def build_niche_url(
    school_name: str | None,
    city: str | None,
    state: str | None,
) -> str | None:
    """Build a Niche.com school URL.
    
    Example:
        build_niche_url("Belvedere Elementary School", "Falls Church", "VA")
        -> "https://www.niche.com/k12/belvedere-elementary-school-falls-church-va/"
    
    URL format: https://www.niche.com/k12/{school-name-slug}-{city-slug}-{state-abbrev}/
    """
    if not school_name or not city or not state:
        return None
    
    # Slugify school name (lowercase, replace spaces and special chars with dashes)
    name_slug = re.sub(r'[^a-z0-9]+', '-', school_name.lower()).strip('-')
    
    # Slugify city (lowercase, replace spaces with dashes)
    city_slug = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
    
    # State abbreviation lowercase
    state_lower = state.strip().lower()
    
    return f"https://www.niche.com/k12/{name_slug}-{city_slug}-{state_lower}/"


def build_greatschools_search_url(
    school_name: str | None,
    city: str | None,
    state: str | None,
    lat: float | None = None,
    lon: float | None = None,
    distance: int = 15,
) -> str | None:
    """Build a GreatSchools best-schools search URL for a school.
    
    Example:
        build_greatschools_search_url("Belvedere Elementary School", "Falls Church", "VA", 38.8838, -77.1746)
        -> "https://www.greatschools.org/best-schools/virginia/falls-church?distance=15&lat=38.8838&locationType=city&lon=-77.1746&q=Belvedere+Elementary+School"
    
    This format includes lat/lon for more precise location-based results.
    """
    if not school_name:
        return None
    
    # Get full state name
    state_upper = (state or "").strip().upper()
    state_full = STATE_FULL_NAMES.get(state_upper)
    if not state_full:
        return None
    
    # Slugify city (lowercase, replace spaces with dashes)
    city_slug = re.sub(r'[^a-z0-9]+', '-', (city or "").lower()).strip('-')
    if not city_slug:
        city_slug = "search"
    
    # Encode school name for query parameter
    encoded_query = quote_plus(school_name)
    
    # Build URL with lat/lon if available
    if lat is not None and lon is not None:
        return (
            f"https://www.greatschools.org/best-schools/{state_full}/{city_slug}"
            f"?distance={distance}&lat={lat:.4f}&locationType=city&lon={lon:.4f}&q={encoded_query}"
        )
    else:
        return (
            f"https://www.greatschools.org/best-schools/{state_full}/{city_slug}"
            f"?q={encoded_query}"
        )


# Common school name suffixes to strip for better matching
SCHOOL_SUFFIXES = [
    " school for the arts and sciences",
    " school for math science and technology",
    " elementary school",
    " middle school",
    " high school",
    " primary school",
    " secondary school",
    " preparatory school",
    " prep school",
    " magnet school",
    " charter school",
    " academy",
    " elementary",
    " middle",
    " high",
    " school",
    " es",  # Elementary School abbreviation
    " ms",  # Middle School abbreviation
    " hs",  # High School abbreviation
]


def normalize_name(value: str | None) -> str:
    """Basic name normalization: lowercase, strip whitespace, remove quotes."""
    if not value:
        return ""
    cleaned = " ".join(value.strip().lower().split())
    cleaned = cleaned.replace("'", "").replace("\"", "")
    return cleaned


def normalize_school_name(value: str | None) -> str:
    """Normalize school name by removing common suffixes for better matching.
    
    Examples:
        "Belvedere Elementary School" -> "belvedere"
        "Belvedere Elementary" -> "belvedere"
        "Falls Church High School" -> "falls church"
        "Bailey's Elementary School for the Arts and Sciences" -> "baileys"
    """
    if not value:
        return ""
    cleaned = normalize_name(value)
    
    # Remove common suffixes (order matters - longer suffixes first)
    for suffix in SCHOOL_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)].strip()
            # Don't break - may have multiple suffixes like "elementary school"
    
    return cleaned


def fuzzy_match_score(a: str | None, b: str | None) -> float:
    """Calculate fuzzy match score between two school names.
    
    Uses normalized school names (without common suffixes) to improve matching.
    """
    if not a or not b:
        return 0.0
    
    # First try with normalized school names (suffixes stripped)
    norm_a = normalize_school_name(a)
    norm_b = normalize_school_name(b)
    
    if norm_a and norm_b:
        normalized_score = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
        # Also calculate basic score for comparison
        basic_score = difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()
        # Return the higher of the two scores
        return max(normalized_score, basic_score)
    
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


# Minimum score to accept a match - prevents false positives
MIN_MATCH_SCORE = 0.75


def match_schooldigger_to_places(
    places: list[dict],
    digger: list[dict],
    *,
    distance_threshold_miles: float = 1.0,
    score_threshold: float = 0.75,
) -> tuple[dict[str, dict], dict[str, float]]:
    """Match Google Places schools to SchoolDigger schools.
    
    Matching criteria:
    1. NCES ID exact match (if available) - score 1.0
    2. Name fuzzy match with score >= score_threshold
    3. Must be within distance_threshold_miles
    4. City/State must match if available
    
    Returns:
        Tuple of (matches dict, match_scores dict)
        - matches: {place_id: matched_school_dict}
        - match_scores: {place_id: match_score}
    
    Note: Only matches with score >= MIN_MATCH_SCORE are returned to prevent
    false positives like "Aqua-Tots Swim School" matching to "Belvedere Elementary".
    """
    matches: dict[str, dict] = {}
    match_scores: dict[str, float] = {}
    
    for place in places:
        place_id = place.get("place_id")
        if not place_id:
            continue
        
        best = None
        best_score = 0.0
        
        for school in digger:
            # Check for exact NCES ID match first
            if school.get("nces_id") and place.get("nces_id"):
                if str(school.get("nces_id")) == str(place.get("nces_id")):
                    best = school
                    best_score = 1.0
                    break
            
            # Skip if state doesn't match
            if place.get("state") and school.get("state"):
                if normalize_name(place.get("state")) != normalize_name(school.get("state")):
                    continue
            
            # Skip if city doesn't match
            if place.get("city") and school.get("city"):
                if normalize_name(place.get("city")) != normalize_name(school.get("city")):
                    continue
            
            # Calculate name similarity score
            score = fuzzy_match_score(place.get("name"), school.get("name"))
            
            # Skip if score is below threshold - no false positives allowed
            if score < score_threshold:
                continue
            
            # Calculate distance if coordinates available
            distance = None
            if place.get("lat") is not None and place.get("lon") is not None:
                if school.get("lat") is not None and school.get("lon") is not None:
                    distance = haversine_distance_miles(
                        place["lat"], place["lon"], school["lat"], school["lon"]
                    )
            
            # Skip if too far away
            if distance is not None and distance > distance_threshold_miles:
                continue
            
            # Bonus for closer schools (small boost)
            if distance is not None:
                score += max(0.0, (distance_threshold_miles - distance) * 0.05)
            
            # Update best match if this is better
            if score > best_score:
                best = school
                best_score = score
        
        # Only accept matches that meet the minimum score threshold
        # This prevents false positives from low-quality matches
        if best and best_score >= MIN_MATCH_SCORE:
            matches[place_id] = best
            match_scores[place_id] = best_score
    
    return matches, match_scores


def clip_geojson_to_radius(
    geojson: dict,
    *,
    center_lat: float,
    center_lon: float,
    radius_miles: float,
) -> list[dict]:
    if not geojson:
        return []
    project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
    house_point_m = transform(project, Point(center_lon, center_lat))
    radius_meters = radius_miles * 1609.34
    search_area = house_point_m.buffer(radius_meters)

    features = []
    for feature in geojson.get("features", []):
        geom = feature.get("geometry")
        if not geom:
            continue
        polygon_m = transform(project, shape(geom))
        if polygon_m.intersects(search_area):
            clipped_geom = polygon_m.intersection(search_area)
            if clipped_geom.is_empty:
                continue
            features.append({
                "type": "Feature",
                "properties": feature.get("properties") or {},
                "geometry": transform(
                    pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform,
                    clipped_geom,
                ).__geo_interface__,
            })
    return features


def grade_range(low: str | None, high: str | None) -> str:
    low_val = (low or "").strip()
    high_val = (high or "").strip()
    if low_val and high_val:
        return f"{low_val}–{high_val}"
    return low_val or high_val or ""


def haversine_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))
