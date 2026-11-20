"""Summarise catalog place IDs using cached iNaturalist place metadata."""

import gzip
import json
import math
import sys
import urllib.request
from collections import Counter
from pathlib import Path

if __package__:
    from .build_search_index import write_gzip
else:
    from build_search_index import write_gzip


COUNTRY_PLACES_PATH = Path(__file__).with_name("country-places.json")
PLACES_API = "https://api.inaturalist.org/v1/places/"
PLACE_BATCH_SIZE = 200


def place_bounds(item):
    geometry = item.get("bounding_box_geojson") or {}
    try:
        ring = geometry["coordinates"][0]
        west, south = map(float, ring[0])
        east, north = map(float, ring[2])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        return None
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90):
        return None
    return [west, south, east, north]


def place_metadata(item, countries):
    place_id = int(item["id"])
    if place_id in countries:
        return {"bounds": place_bounds(item)}
    if item.get("admin_level") != 10:
        return None
    ancestors = sorted(set(item.get("ancestor_place_ids") or []) & countries.keys())
    if not ancestors:
        return None
    location = item.get("location")
    try:
        lat, lon = map(float, location.split(","))
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates")
    except (AttributeError, ValueError, TypeError):
        lat = lon = None
    bounds = place_bounds(item)
    if lat is not None and bounds and bounds[0] > bounds[2]:
        lon = ((bounds[0] + (bounds[2] + 360 - bounds[0]) / 2 + 180) % 360) - 180
    if not item.get("name"):
        raise ValueError(f"Missing name for first-level place {place_id}.")
    return {"name": item["name"], "lat": lat, "lon": lon, "countries": ancestors}


def build_country_map(connection, output_dir, request_json, previous_url=None):
    with COUNTRY_PLACES_PATH.open(encoding="utf-8") as file:
        country_places = json.load(file)
    countries = {item["id"]: item for item in country_places}
    if len(countries) != len(country_places):
        raise ValueError("Duplicate country place IDs")

    cache = {}
    if previous_url:
        with urllib.request.urlopen(previous_url, timeout=180) as response:
            with gzip.GzipFile(fileobj=response) as compressed:
                cached = json.load(compressed)
        if (cached.get("schema") != 1 or not isinstance(cached.get("places"), dict)
                or not isinstance(cached.get("country_ids"), list)):
            raise ValueError("The previous place metadata cache is invalid.")
        if cached["country_ids"] == sorted(countries):
            cache = {int(key): value for key, value in cached["places"].items()}
        else:
            print("Country places changed; refreshing place metadata.", flush=True)

    needed = set()
    for (payload,) in connection.execute("SELECT payload FROM observations"):
        record = json.loads(payload)
        needed.update(record.get("place_ids") or [])
    missing = sorted(needed - cache.keys())
    for offset in range(0, len(missing), PLACE_BATCH_SIZE):
        batch = missing[offset:offset + PLACE_BATCH_SIZE]
        data = request_json(PLACES_API + ",".join(map(str, batch)), api_request=True)
        if not isinstance(data.get("results"), list):
            raise ValueError("Invalid response from the iNaturalist place API.")
        for item in data["results"]:
            place_id = int(item["id"])
            if place_id not in batch or place_id in cache:
                raise ValueError("Unexpected or duplicate place in iNaturalist response.")
            cache[place_id] = place_metadata(item, countries)
        if offset % 2000 == 0:
            print(f"Looked up {min(offset + len(batch), len(missing))} of {len(missing)} places.", flush=True)

    unresolved = needed - cache.keys()
    if unresolved:
        print(f"Warning: {len(unresolved)} places could not be retrieved; they will be retried.", file=sys.stderr)

    country_counts = Counter()
    region_counts = Counter()
    country_unassigned_regions = Counter()
    unassigned = 0
    total = 0
    for (payload,) in connection.execute("SELECT payload FROM observations"):
        places = set(json.loads(payload).get("place_ids") or [])
        total += 1
        matched_countries = places & countries.keys()
        if not matched_countries:
            unassigned += 1
        country_counts.update(matched_countries)
        matched_regions = {place_id: cache[place_id] for place_id in places
                           if place_id in cache and cache[place_id]
                           and "countries" in cache[place_id]}
        for country_id in matched_countries:
            regions = [place_id for place_id, metadata in matched_regions.items()
                       if country_id in metadata["countries"]]
            if not regions:
                country_unassigned_regions[country_id] += 1
            region_counts.update((country_id, place_id) for place_id in regions)

    results = []
    for country_id, count in sorted(country_counts.items()):
        regions = []
        for (parent_id, place_id), region_count in sorted(region_counts.items()):
            if parent_id == country_id:
                metadata = cache[place_id]
                regions.append({key: metadata[key] for key in ("name", "lat", "lon")} |
                               {"id": place_id, "count": region_count})
        metadata = cache.get(country_id) or {}
        results.append({**countries[country_id], "count": count, "bounds": metadata.get("bounds"),
                        "unassigned_regions": country_unassigned_regions[country_id], "regions": regions})
    write_gzip(output_dir / "country-map.json.gz", {
        "schema": 2, "total": total, "unassigned": unassigned,
        "missing_places": len(unresolved), "countries": results,
    })
    write_gzip(output_dir / "place-metadata.json.gz", {
        "schema": 1, "country_ids": sorted(countries),
        "places": {str(key): cache[key] for key in sorted(needed & cache.keys())},
    })
