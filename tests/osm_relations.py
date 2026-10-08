"""Test helper for FA3/FA46: Overpass relation elements as `out geom` returns them
(members are ways with geometry), built from rings given as coordinate lists.
Shared by tests/unit/test_fa03_osm_relations.py and the Sentinel-2 example e2e test."""

from __future__ import annotations


def way(ref: int, coords, role: str = "outer", reverse: bool = False) -> dict:
    points = list(reversed(coords)) if reverse else list(coords)
    return {
        "type": "way",
        "ref": ref,
        "role": role,
        "geometry": [{"lat": lat, "lon": lon} for lon, lat in points],
    }


def ring_ways(ring, parts: int, first_ref: int, role: str = "outer") -> list[dict]:
    """A closed ring (lon, lat; first == last) cut into `parts` ways; every other way is
    stored reversed, as OSM contributors do."""
    count = len(ring) - 1
    cuts = [round(i * count / parts) for i in range(parts + 1)]
    ways = []
    for index in range(parts):
        piece = ring[cuts[index] : cuts[index + 1] + 1]
        ways.append(way(first_ref + index, piece, role, reverse=index % 2 == 1))
    return ways


def square(x0, y0, size, steps: int = 4):
    """Closed ring of a square with `steps` vertices per side."""
    ring = []
    for i in range(steps):
        ring.append((x0 + size * i / steps, y0))
    for i in range(steps):
        ring.append((x0 + size, y0 + size * i / steps))
    for i in range(steps):
        ring.append((x0 + size - size * i / steps, y0 + size))
    for i in range(steps):
        ring.append((x0, y0 + size - size * i / steps))
    ring.append(ring[0])
    return ring


def rectangle(x0, y0, width, height, steps: int = 4):
    """Closed ring of a rectangle with `steps` vertices per side."""
    ring = []
    for i in range(steps):
        ring.append((x0 + width * i / steps, y0))
    for i in range(steps):
        ring.append((x0 + width, y0 + height * i / steps))
    for i in range(steps):
        ring.append((x0 + width - width * i / steps, y0 + height))
    for i in range(steps):
        ring.append((x0, y0 + height - height * i / steps))
    ring.append(ring[0])
    return ring


def relation(rid: int, members: list[dict], **tags) -> dict:
    return {
        "type": "relation",
        "id": rid,
        "tags": {
            "type": "boundary",
            "boundary": "administrative",
            "name": f"Gebiet {rid}",
            **tags,
        },
        "members": members,
    }
