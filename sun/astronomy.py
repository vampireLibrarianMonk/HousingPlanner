from datetime import date, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun, azimuth


def compute_season_azimuths(lat: float, lon: float, tz_name: str) -> dict:
    seasons = {
        "Winter": date(2025, 12, 21),
        "Equinox": date(2025, 3, 20),
        "Summer": date(2025, 6, 21),
    }

    results = {}

    for season, d in seasons.items():
        loc = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name)
        s = sun(loc.observer, date=d, tzinfo=ZoneInfo(tz_name))

        azimuths = []
        t = s["sunrise"]
        while t <= s["sunset"]:
            azimuths.append(azimuth(loc.observer, t))
            t += timedelta(minutes=10)

        results[season] = azimuths

    return results
