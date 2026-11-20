"""Convert Natural Earth admin-1 lines to the map's equirectangular SVG."""

import json
import sys
from pathlib import Path


def simplify(points, tolerance=0.06):
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start, end = pending.pop()
        x1, y1 = points[start]
        x2, y2 = points[end]
        dx, dy = x2 - x1, y2 - y1
        length_squared = dx * dx + dy * dy
        furthest, index = tolerance * tolerance, None
        for i in range(start + 1, end):
            x, y = points[i]
            fraction = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / length_squared)) if length_squared else 0
            distance = (x - x1 - fraction * dx) ** 2 + (y - y1 - fraction * dy) ** 2
            if distance > furthest:
                furthest, index = distance, i
        if index is not None:
            keep.add(index)
            pending.extend(((start, index), (index, end)))
    return [points[i] for i in sorted(keep)]


def paths(geojson):
    for feature in geojson["features"]:
        geometry = feature.get("geometry")
        kind = feature["properties"].get("featurecla") or feature["properties"].get("FEATURECLA")
        if not geometry or kind in ("Admin-1 statistical meta bounds", "Admin-1 boundary indicator"):
            continue
        if geometry["type"] == "LineString":
            lines = [geometry["coordinates"]]
        elif geometry["type"] == "MultiLineString":
            lines = geometry["coordinates"]
        else:
            raise ValueError(f"Unexpected boundary geometry: {geometry['type']}")
        for line in lines:
            segment = []
            previous_lon = None
            for lon, lat, *_ in line:
                if previous_lon is not None and abs(lon - previous_lon) > 180:
                    if len(segment) > 1:
                        yield simplify(segment)
                    segment = []
                segment.append(((lon + 180) * 960 / 360, (90 - lat) * 480 / 180))
                previous_lon = lon
            if len(segment) > 1:
                yield simplify(segment)


def main(source, destination):
    with Path(source).open(encoding="utf-8") as file:
        geojson = json.load(file)
    segments = (
        "M" + "L".join(f"{x:.2f},{y:.2f}" for x, y in segment)
        for segment in paths(geojson)
    )
    Path(destination).write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 480">'
        '<path id="state-borders" d="' + "".join(segments) + '"/></svg>\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python build_admin_boundaries.py INPUT.geojson OUTPUT.svg")
    main(sys.argv[1], sys.argv[2])
