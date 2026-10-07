"""Administrative boundary search via OpenStreetMap Nominatim.

Used for Option C of AOI selection: "Auchi, Etsako West LGA, Edo State, Nigeria".
Only returns results that Nominatim actually reports (no invented places).
"""

from __future__ import annotations

from typing import Optional

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "AIGISMapGenerator/0.1 (research prototype; contact: local-user)"


class AdminSearchError(RuntimeError):
    pass


def search_administrative_area(query: str, limit: int = 6,
                               countrycodes: Optional[str] = None) -> list[dict]:
    """Search an administrative place name -> list of {display_name, bbox, geojson}."""
    if not query or not query.strip():
        raise AdminSearchError("Search query is empty.")
    params = {
        "q": query.strip(),
        "format": "jsonv2",
        "polygon_geojson": 1,
        "addressdetails": 1,
        "limit": limit,
    }
    if countrycodes:
        params["countrycodes"] = countrycodes
    try:
        resp = requests.get(NOMINATIM_URL, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=30)
    except requests.RequestException as exc:
        raise AdminSearchError(
            f"Nominatim request failed: {exc}. Recommended action: retry later."
        ) from exc
    if resp.status_code != 200:
        raise AdminSearchError(
            f"Nominatim returned HTTP {resp.status_code}. "
            "Recommended action: retry later (rate limits apply)."
        )
    results = []
    for item in resp.json():
        bbox = [
            float(item["boundingbox"][2]), float(item["boundingbox"][0]),  # minLon, minLat
            float(item["boundingbox"][3]), float(item["boundingbox"][1]),  # maxLon, maxLat
        ]
        entry = {
            "display_name": item.get("display_name", query),
            "type": item.get("type"),
            "category": item.get("class"),
            "osm_type": item.get("osm_type"),
            "osm_id": item.get("osm_id"),
            "bbox": bbox,
            "geometry": None,
            "source": "OpenStreetMap Nominatim",
        }
        geom = item.get("geojson")
        if geom and geom.get("type") in ("Polygon", "MultiPolygon"):
            entry["geometry"] = geom
        results.append(entry)
    return results


def reverse_geocode(lon: float, lat: float) -> Optional[dict]:
    """Reverse lookup of an address for a coordinate (display only)."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lon": lon, "lat": lat, "format": "jsonv2"},
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"display_name": data.get("display_name"), "address": data.get("address", {})}
    except requests.RequestException:
        return None
    return None
