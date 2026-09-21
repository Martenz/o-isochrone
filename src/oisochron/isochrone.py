"""Compute a travel-time isochrone and the reachable road network."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import geopandas as gpd
import h3
import networkx as nx
import osmnx as ox
from shapely import concave_hull, segmentize
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from oisochron.config import Settings, get_settings
from oisochron.exceptions import IsochroneError
from oisochron.geojson import feature, feature_collection
from oisochron.network import load_road_network
from oisochron.speeds import (
    TravelType,
    max_distance_meters,
    parse_travel_type,
    speed_kph,
)

_DEFAULT_BUFFER_METERS = 10.0
_CONCAVE_HULL_RATIO = 0.3
_DEFAULT_H3_RESOLUTION = 10

INCLUDE_LAYERS = (
    "lines",
    "buffered_lines",
    "filled_buffer",
    "convex_hull",
    "concave_hull",
    "h3",
)
_INCLUDE_ALIASES = {
    "both": ("lines", "filled_buffer"),
    "polygon": ("filled_buffer",),
    "isochrone": ("filled_buffer",),
    "network": ("lines",),
    "network_buffer": ("buffered_lines",),
    "polygon+lines": ("lines", "filled_buffer"),
}


def compute_isochrone(
    lat: float,
    lon: float,
    travel_time_min: float,
    travel_type: str | TravelType,
    *,
    include: str | Iterable[str] | None = None,
    buffer_meters: float | None = None,
    h3_resolution: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Download the nearby OSM roads and compute an isochrone.

    Parameters
    ----------
    lat, lon:
        Origin in WGS84.
    travel_time_min:
        Maximum travel time in minutes.
    travel_type:
        ``foot`` / ``feet``, ``car``, or ``bicycle`` / ``bicicle``.
        The configured average speed for that mode is used as edge cost.
    include:
        One or more of ``lines``, ``buffered_lines``, ``filled_buffer``,
        ``convex_hull``, ``concave_hull``, ``h3``. Default ``buffered_lines``.
        Legacy values ``both``, ``polygon``, and ``lines`` are still accepted.
    buffer_meters:
        Corridor width for ``buffered_lines`` and ``filled_buffer`` (default 10).
    h3_resolution:
        Uber H3 resolution (0–15) when ``h3`` is included. Default 10.

    Returns
    -------
    GeoJSON FeatureCollection with one feature per requested layer.
    """
    settings = settings or get_settings()
    mode = parse_travel_type(travel_type)
    layers = parse_include(include)
    travel_time_min = _validate_travel_time(travel_time_min, settings)

    dist_m = max_distance_meters(mode, travel_time_min, settings)
    graph = load_road_network(lat, lon, dist_m, mode, settings)
    return isochrone_from_graph(
        graph,
        lat,
        lon,
        travel_time_min,
        mode,
        include=layers,
        buffer_meters=buffer_meters,
        h3_resolution=h3_resolution,
        settings=settings,
    )


def isochrone_from_graph(
    graph,
    lat: float,
    lon: float,
    travel_time_min: float,
    travel_type: str | TravelType,
    *,
    include: str | Iterable[str] | None = None,
    buffer_meters: float | None = None,
    h3_resolution: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Compute isochrone GeoJSON from an already-loaded OSMnx graph."""
    settings = settings or get_settings()
    mode = parse_travel_type(travel_type)
    layers = parse_include(include)
    resolution = _parse_h3_resolution(h3_resolution)
    width = _buffer_width(buffer_meters)
    if graph.number_of_nodes() == 0:
        raise IsochroneError("Road network graph is empty.")

    kmh = speed_kph(mode, settings)
    graph = graph.copy()
    _assign_travel_times(graph, kmh)

    origin = ox.distance.nearest_nodes(graph, X=lon, Y=lat)
    radius_s = travel_time_min * 60.0
    reachable = nx.ego_graph(graph, origin, radius=radius_s, distance="travel_time")
    if reachable.number_of_edges() == 0 and reachable.number_of_nodes() == 0:
        raise IsochroneError("No reachable roads from the origin within the travel time.")

    lines = _reachable_lines(reachable)
    if lines.is_empty and reachable.number_of_nodes() == 0:
        raise IsochroneError("No reachable roads from the origin within the travel time.")

    shared = {
        "travel_type": mode.value,
        "travel_time_min": travel_time_min,
        "speed_kph": kmh,
    }
    features: list[dict[str, Any]] = []
    for kind in layers:
        if kind == "h3":
            features.extend(_h3_features(reachable, origin, resolution, shared))
            continue
        geom = _layer_geometry(kind, reachable, lines, width, resolution)
        props: dict[str, Any] = {"kind": kind, **shared}
        if kind in {"buffered_lines", "filled_buffer"}:
            props["buffer_meters"] = width
        if kind == "concave_hull":
            props["concave_ratio"] = _CONCAVE_HULL_RATIO
        features.append(feature(geom, props))

    return feature_collection(
        features,
        metadata={
            "lat": lat,
            "lon": lon,
            "travel_time_min": travel_time_min,
            "travel_type": mode.value,
            "speed_kph": kmh,
            "include": list(layers),
            "buffer_meters": width,
            "h3_resolution": resolution if "h3" in layers else None,
            "origin_node": int(origin) if _is_int_like(origin) else origin,
        },
    )


def parse_include(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize include to a unique tuple of layer names."""
    parts: list[str] = []
    if value is None:
        tokens: list[str] = []
    elif isinstance(value, str):
        tokens = [item.strip() for item in value.replace(";", ",").split(",")]
    else:
        tokens = []
        for item in value:
            tokens.extend(parse_include(item))
        return _dedupe_layers(tokens) or ("buffered_lines",)

    for token in tokens:
        if not token:
            continue
        normalized = token.strip().lower()
        if normalized in _INCLUDE_ALIASES:
            parts.extend(_INCLUDE_ALIASES[normalized])
        elif normalized in INCLUDE_LAYERS:
            parts.append(normalized)
        else:
            allowed = ", ".join(INCLUDE_LAYERS)
            raise ValueError(f"include contains unknown layer {token!r}; use one of: {allowed}")
    if not parts:
        return ("buffered_lines",)
    return _dedupe_layers(parts)


def _dedupe_layers(parts: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for layer in parts:
        if layer in seen:
            continue
        seen.add(layer)
        ordered.append(layer)
    return tuple(ordered)


def _parse_h3_resolution(value: int | None) -> int:
    if value is None:
        return _DEFAULT_H3_RESOLUTION
    try:
        resolution = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("h3_resolution must be an integer") from exc
    if resolution < 0 or resolution > 15:
        raise ValueError("h3_resolution must be between 0 and 15")
    return resolution


def _buffer_width(buffer_meters: float | None) -> float:
    if buffer_meters is None:
        return _DEFAULT_BUFFER_METERS
    try:
        width = float(buffer_meters)
    except (TypeError, ValueError) as exc:
        raise ValueError("buffer_meters must be a number") from exc
    if width < 0:
        raise ValueError("buffer_meters cannot be negative")
    if width == 0:
        return _DEFAULT_BUFFER_METERS
    return width


def _validate_travel_time(travel_time_min: float, settings: Settings) -> float:
    try:
        minutes = float(travel_time_min)
    except (TypeError, ValueError) as exc:
        raise ValueError("travel_time_min must be a number") from exc
    if minutes <= 0:
        raise ValueError("travel_time_min must be greater than 0")
    if minutes > settings.max_travel_time_min:
        raise ValueError(
            f"travel_time_min cannot exceed {settings.max_travel_time_min} minutes"
        )
    return minutes


def _assign_travel_times(graph, kmh: float) -> None:
    meters_per_second = kmh / 3.6
    if meters_per_second <= 0:
        raise ValueError("Average speed must be greater than 0")
    for _, _, _, data in graph.edges(keys=True, data=True):
        length = float(data.get("length") or 0.0)
        if length <= 0 and data.get("geometry") is not None:
            geom = data["geometry"]
            if geom.geom_type in {"LineString", "MultiLineString"}:
                # Graph coordinates are lon/lat; approximate length in meters.
                length = float(gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(3857).length.iloc[0])
        if length > 0:
            data["length"] = length
        data["travel_time"] = length / meters_per_second if length > 0 else float("inf")


def _reachable_lines(graph) -> BaseGeometry:
    lines: list[LineString] = []
    for u, v, _, data in graph.edges(keys=True, data=True):
        geom = data.get("geometry")
        if geom is None:
            u_data, v_data = graph.nodes[u], graph.nodes[v]
            geom = LineString(
                [(float(u_data["x"]), float(u_data["y"])), (float(v_data["x"]), float(v_data["y"]))]
            )
        if geom.is_empty:
            continue
        if geom.geom_type == "MultiLineString":
            lines.extend(g for g in geom.geoms if not g.is_empty)
        elif geom.geom_type == "LineString":
            lines.append(geom)
    if not lines:
        return MultiLineString()
    merged = unary_union(lines)
    if merged.geom_type == "LineString":
        return MultiLineString([merged])
    return merged


def _layer_geometry(
    kind: str,
    graph,
    lines: BaseGeometry,
    buffer_meters: float,
    h3_resolution: int,
) -> BaseGeometry:
    if kind == "lines":
        if lines.is_empty:
            raise IsochroneError("No reachable road geometries within the travel time.")
        return lines
    if kind == "buffered_lines":
        return _buffered_network(graph, lines, buffer_meters)
    if kind == "filled_buffer":
        return _fill_holes(_buffered_network(graph, lines, buffer_meters))
    if kind == "convex_hull":
        return _as_polygon(_source_geometry(graph, lines).convex_hull, buffer_meters)
    if kind == "concave_hull":
        return _concave_hull(graph, lines, buffer_meters)
    if kind == "h3":
        return _h3_cells_to_multipolygon(set(_h3_route_distances(graph, None, h3_resolution)))
    raise ValueError(f"Unknown include layer {kind!r}")


def _source_geometry(graph, lines: BaseGeometry) -> BaseGeometry:
    if not lines.is_empty:
        return lines
    points = [
        Point(float(data["x"]), float(data["y"]))
        for _, data in graph.nodes(data=True)
        if "x" in data and "y" in data
    ]
    if not points:
        raise IsochroneError("Reachable network has no coordinates to build a polygon.")
    if len(points) == 1:
        return points[0]
    if len(points) == 2:
        return LineString(points)
    return unary_union(points)


def _buffered_network(graph, lines: BaseGeometry, buffer_meters: float) -> BaseGeometry:
    source = _source_geometry(graph, lines)
    return _buffer_meters(source, buffer_meters)


def _as_polygon(geom: BaseGeometry, buffer_meters: float) -> BaseGeometry:
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return geom
    return _buffer_meters(geom, buffer_meters)


def _concave_hull(graph, lines: BaseGeometry, buffer_meters: float) -> BaseGeometry:
    source = _source_geometry(graph, lines)
    try:
        hull = concave_hull(source, ratio=_CONCAVE_HULL_RATIO)
    except Exception:
        hull = source.convex_hull
    if hull.is_empty:
        hull = source.convex_hull
    return _as_polygon(hull, buffer_meters)


def _h3_features(graph, origin, resolution: int, shared: dict[str, Any]) -> list[dict[str, Any]]:
    distances = _h3_route_distances(graph, origin, resolution)
    if not distances:
        raise IsochroneError("No H3 cells intersect the reachable network.")
    finite = [value for value in distances.values() if value != float("inf")]
    max_distance = max(finite) if finite else 0.0
    features: list[dict[str, Any]] = []
    for cell, distance in distances.items():
        if distance == float("inf"):
            continue
        geom = _h3_polygon(cell)
        if geom is None or geom.is_empty:
            continue
        progress = 0.0 if max_distance <= 0 else min(distance / max_distance, 1.0)
        features.append(
            feature(
                geom,
                {
                    **shared,
                    "kind": "h3",
                    "h3_index": cell,
                    "h3_resolution": resolution,
                    "route_distance_m": round(distance, 3),
                    "progress": round(progress, 6),
                },
                feature_id=cell,
            )
        )
    if not features:
        raise IsochroneError("No H3 cells intersect the reachable network.")
    return features


def _h3_route_distances(graph, origin, resolution: int) -> dict[str, float]:
    """Minimum network distance from the origin to each H3 cell the roads touch."""
    step_m = _h3_sample_step_meters(resolution)
    if origin is None:
        origin = next(iter(graph.nodes), None)
    dist_m: dict[Any, float] = {}
    if origin is not None and origin in graph:
        dist_m = nx.single_source_dijkstra_path_length(graph, origin, weight="length")
        dist_m.setdefault(origin, 0.0)

    cell_dist: dict[str, float] = {}

    def visit(cell: str, distance: float) -> None:
        previous = cell_dist.get(cell)
        if previous is None or distance < previous:
            cell_dist[cell] = distance

    for node, data in graph.nodes(data=True):
        if "x" not in data or "y" not in data:
            continue
        visit(
            h3.latlng_to_cell(float(data["y"]), float(data["x"]), resolution),
            float(dist_m.get(node, 0.0)),
        )

    for u, v, _, data in graph.edges(keys=True, data=True):
        line = data.get("geometry")
        if line is None:
            u_data, v_data = graph.nodes[u], graph.nodes[v]
            if "x" not in u_data or "x" not in v_data:
                continue
            line = LineString(
                [(float(u_data["x"]), float(u_data["y"])), (float(v_data["x"]), float(v_data["y"]))]
            )
        length = float(data.get("length") or 0.0)
        d_u = dist_m.get(u)
        d_v = dist_m.get(v)
        if d_u is None and d_v is None:
            continue
        u_point = None
        if "x" in graph.nodes[u] and "y" in graph.nodes[u]:
            u_point = Point(float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"]))
        for part in _line_parts(line):
            coords = _coords_along_line(part, step_m)
            if not coords:
                continue
            n = max(len(coords) - 1, 1)
            starts_at_u = True
            if u_point is not None:
                start = Point(coords[0])
                end = Point(coords[-1])
                starts_at_u = start.distance(u_point) <= end.distance(u_point)
            previous: str | None = None
            previous_distance = 0.0
            for i, (lon, lat) in enumerate(coords):
                t = i / n
                if not starts_at_u:
                    t = 1.0 - t
                candidates: list[float] = []
                if d_u is not None:
                    candidates.append(d_u + t * length)
                if d_v is not None:
                    candidates.append(d_v + (1.0 - t) * length)
                distance = min(candidates) if candidates else 0.0
                cell = h3.latlng_to_cell(lat, lon, resolution)
                visit(cell, distance)
                if previous is not None and previous != cell:
                    path = list(h3.grid_path_cells(previous, cell))
                    for index, mid in enumerate(path):
                        frac = index / max(len(path) - 1, 1)
                        visit(mid, previous_distance + frac * (distance - previous_distance))
                previous = cell
                previous_distance = distance
    return cell_dist


def _h3_polygon(cell: str) -> Polygon | None:
    ring = [(lng, lat) for lat, lng in h3.cell_to_boundary(cell)]
    if not ring:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    poly = Polygon(ring)
    return None if poly.is_empty else poly


def _h3_cells_to_multipolygon(cells: set[str]) -> BaseGeometry:
    polygons = [poly for cell in cells if (poly := _h3_polygon(cell)) is not None]
    if not polygons:
        raise IsochroneError("No H3 cells intersect the reachable network.")
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def _h3_sample_step_meters(resolution: int) -> float:
    edge_m = float(h3.average_hexagon_edge_length(resolution, unit="m"))
    return max(edge_m / 3.0, 1.0)


def _line_parts(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]  # type: ignore[list-item]
    if geom.geom_type == "MultiLineString":
        return [part for part in geom.geoms if not part.is_empty]
    if geom.geom_type == "GeometryCollection":
        parts: list[LineString] = []
        for part in geom.geoms:
            parts.extend(_line_parts(part))
        return parts
    return []


def _coords_along_line(line: LineString, step_m: float) -> list[tuple[float, float]]:
    gdf = gpd.GeoDataFrame(geometry=[line], crs="EPSG:4326")
    projected = ox.projection.project_gdf(gdf)
    dense = segmentize(projected.geometry.iloc[0], max_segment_length=step_m)
    lonlat = gpd.GeoSeries([dense], crs=projected.crs).to_crs("EPSG:4326").iloc[0]
    return [(float(x), float(y)) for x, y, *_ in lonlat.coords]


def _fill_holes(geom: BaseGeometry) -> BaseGeometry:
    """Keep the outer envelope so city blocks fill in, without dropping roads."""
    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return unary_union([Polygon(part.exterior) for part in geom.geoms])
    return geom


def _isochrone_polygon(
    graph,
    lines: BaseGeometry,
    buffer_meters: float | None = None,
) -> BaseGeometry:
    """Filled corridor of the reachable network (buffer + interior holes removed)."""
    return _fill_holes(_buffered_network(graph, lines, _buffer_width(buffer_meters)))


def _buffer_meters(geom: BaseGeometry, meters: float) -> BaseGeometry:
    series = gpd.GeoSeries([geom], crs="EPSG:4326")
    projected = ox.projection.project_gdf(gpd.GeoDataFrame(geometry=series))
    buffered = projected.buffer(meters).to_crs("EPSG:4326").union_all()
    if buffered.is_empty:
        raise IsochroneError("Buffer of the reachable network is empty.")
    return buffered


def _is_int_like(value: object) -> bool:
    try:
        int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True
