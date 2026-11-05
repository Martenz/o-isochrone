"""Edge lengths and travel times for an OSMnx road graph.

The road graph carries an OSM ``length`` attribute (meters, computed by OSMnx
from the OSM geometry) for almost every edge, so travel-time assignment usually
only has to divide it by a speed. Two fallbacks handle the rest:

1. the edge ``geometry`` (a great-circle sum along its coordinates);
2. the edge endpoints (a great-circle distance between the two nodes).

Both are metric and latitude-correct. Earlier versions used flat approximations
(``degrees * 111_320`` and a Web Mercator length), which overestimate an
east-west edge by ``1 / cos(latitude)`` — +42.7% in Milan, +100% at 60° — and
left the third of a real OSMnx graph whose short edges carry no ``geometry``
with an infinite travel time, silently dropping those edges from the isochrone.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from shapely.geometry.base import BaseGeometry

# Same mean Earth radius OSMnx uses for its own great-circle helpers, so a
# derived length stays comparable with the ``length`` OSMnx writes itself.
EARTH_RADIUS_M = 6_371_009.0


def great_circle_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in meters between two lon/lat points (haversine)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lon2 - lon1)
    h = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def coordinates_length_m(coords: Iterable[Any]) -> float | None:
    """Great-circle length of a lon/lat coordinate sequence (``None`` if unusable)."""
    points = [(float(point[0]), float(point[1])) for point in coords]
    if len(points) < 2:
        return None
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in pairwise(points):
        total += great_circle_m(lon1, lat1, lon2, lat2)
    return total


def geometry_length_m(geometry: BaseGeometry | None) -> float | None:
    """Great-circle length of a (Multi)LineString in lon/lat degrees."""
    if geometry is None or getattr(geometry, "is_empty", True):
        return None
    geom_type = getattr(geometry, "geom_type", "")
    parts = getattr(geometry, "geoms", ())
    if geom_type == "LineString":
        return coordinates_length_m(geometry.coords)
    if geom_type == "MultiLineString":
        lengths = [coordinates_length_m(part.coords) for part in parts]
        usable = [length for length in lengths if length is not None]
        return sum(usable) if usable else None
    if geom_type == "GeometryCollection":
        lengths = [geometry_length_m(part) for part in parts]
        usable = [length for length in lengths if length is not None]
        return sum(usable) if usable else None
    return None


def _positive_length(value: Any) -> float | None:
    """Return *value* as a positive float, or ``None`` when it is unusable."""
    if value is None:
        return None
    try:
        length = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(length) or length <= 0:
        return None
    return length


def node_pair_length_m(graph: Any, u: Any, v: Any) -> float | None:
    """Great-circle distance between two graph nodes, or ``None`` without coords."""
    try:
        start = graph.nodes[u]
        end = graph.nodes[v]
        return great_circle_m(
            float(start["x"]), float(start["y"]), float(end["x"]), float(end["y"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def edge_length_m(graph: Any, u: Any, v: Any, data: dict[str, Any]) -> float | None:
    """Metric length of one edge: OSM ``length``, then geometry, then endpoints."""
    length = _positive_length(data.get("length"))
    if length is not None:
        return length
    length = geometry_length_m(data.get("geometry"))
    if length is not None and length > 0:
        return length
    return node_pair_length_m(graph, u, v)


def assign_travel_times(graph: Any, kmh: float) -> None:
    """Stamp every edge with a metric ``length`` and a ``travel_time`` (seconds).

    Edges whose length cannot be measured at all keep ``travel_time = inf`` so
    graph traversal skips them instead of treating them as free.
    """
    try:
        meters_per_second = float(kmh) / 3.6
    except (TypeError, ValueError) as exc:
        raise ValueError("Average speed must be a number") from exc
    if not math.isfinite(meters_per_second) or meters_per_second <= 0:
        raise ValueError("Average speed must be greater than 0")

    for u, v, _, data in graph.edges(keys=True, data=True):
        length = edge_length_m(graph, u, v, data)
        if length is None:
            data["travel_time"] = float("inf")
            continue
        data["length"] = length
        data["travel_time"] = length / meters_per_second
