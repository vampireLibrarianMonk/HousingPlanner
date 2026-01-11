from geopy.geocoders import Nominatim

def geocode_once(address: str) -> tuple[float, float]:
    geolocator = Nominatim(
        user_agent="house-planner-prototype",
        timeout=5,
    )
    location = geolocator.geocode(address)
    if not location:
        raise RuntimeError(f"Could not geocode address: {address}")
    return location.latitude, location.longitude
