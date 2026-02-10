import polyline


def decode_geometry(geometry, provider):
    """
    Returns list of (lat, lon)
    """
    if not geometry:
        return []

    if provider == "ORS":
        # ORS may return encoded polyline OR GeoJSON coordinates
        if isinstance(geometry, str):
            return polyline.decode(geometry)

        # GeoJSON-style [[lon, lat], ...]
        return [(lat, lon) for lon, lat in geometry]

    if provider == "GOOGLE":
        return polyline.decode(geometry)

    if provider == "WAZE":
        return polyline.decode(geometry)

    return []