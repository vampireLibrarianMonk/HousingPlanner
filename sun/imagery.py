import io
import math
import time
import requests
from PIL import Image
import streamlit as st


@st.cache_data(show_spinner=False, ttl=86400)
def get_static_osm_image(lat, lon, zoom=19, size=800, cache_buster=None):
    tile_size = 256
    half = size // 2

    def latlon_to_pixel(lat_deg, lon_deg, zoom):
        """
        Convert lat/lon to global pixel coordinates.
        """
        lat_rad = math.radians(lat_deg)
        n = 2.0 ** zoom

        x = (lon_deg + 180.0) / 360.0 * n * tile_size
        y = (
            (1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi)
            / 2.0
            * n
            * tile_size
        )
        return x, y

    # Global pixel coordinate of the house
    px, py = latlon_to_pixel(lat, lon, zoom)

    # Top-left pixel we want to start drawing from
    start_x = int(px - half)
    start_y = int(py - half)

    # Tile indices covering the required area
    x0_tile = start_x // tile_size
    y0_tile = start_y // tile_size

    x1_tile = (start_x + size) // tile_size
    y1_tile = (start_y + size) // tile_size

    img = Image.new("RGB", (size, size))

    for xtile in range(x0_tile, x1_tile + 1):
        for ytile in range(y0_tile, y1_tile + 1):
            url = (
                "https://services.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/"
                f"{zoom}/{ytile}/{xtile}"
            )

            headers = {"User-Agent": "House-Planner-Prototype/1.0"}
            tile = None
            last_error = None

            for attempt in range(3):
                try:
                    resp = requests.get(url, headers=headers, timeout=20)
                    resp.raise_for_status()
                    tile = Image.open(io.BytesIO(resp.content))
                    break
                except requests.RequestException as exc:
                    last_error = exc
                    time.sleep(0.4 * (attempt + 1))

            if tile is None:
                # Skip the tile if imagery is temporarily unavailable.
                # Leave the base image blank for this tile to avoid failing the map.
                continue

            # Pixel offset of this tile relative to image origin
            paste_x = xtile * tile_size - start_x
            paste_y = ytile * tile_size - start_y

            img.paste(tile, (paste_x, paste_y))

    return img
