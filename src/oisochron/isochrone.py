"""Compute a travel-time isochrone and the reachable road network."""

from __future__ import annotations

from typing import Any, Literal

import geopandas as gpd
import networkx as nx
import osmnx as ox
from shapely import concave_hull
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point
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

Include = Literal["both", "polygon", "lines"]


def compute_isochrone(
    lat: float,
    lon: float,
    travel_time_min: float,
    travel_type: str | TravelType,
    *,
    include: Include = "both",
    buffer_meters: float | None = None,
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
        ``polygon`` (isochrone area), ``lines`` (reachable roads), or ``both``.
    buffer_meters:
        If set, replace the road lines with a metric buffer of the reachable
        network (Polygon or MultiPolygon). Ignored when ``include="polygon"``.

    Returns
    -------
    GeoJSON FeatureCollection with ``isochrone`` and/or ``network`` features.
    """
    settings = settings or get_settings()
    mode = parse_travel_type(travel_type)
    include = _parse_include(include)
    travel_time_min = _validate_travel_time(travel_time_min, settings)

    dist_m = max_distance_meters(mode, travel_time_min, settings)
    graph = load_road_network(lat, lon, dist_m, mode, settings)
    return isochrone_from_graph(
        graph,
        lat,
        lon,
        travel_time_min,
        mode,
        include=include,
        buffer_meters=buffer_meters,
        settings=settings,
    )


def isochrone_from_graph(
    graph,
    lat: float,
    lon: float,
    travel_time_min: float,
    travel_type: str | TravelType,
    *,
    include: Include = "both",
    buffer_meters: float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Compute isochrone GeoJSON from an already-loaded OSMnx graph."""
    settings = settings or get_settings()
    mode = parse_travel_type(travel_type)
    include = _parse_include(include)
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

    features: list[dict[str, Any]] = []
    if include in {"both", "polygon"}:
        polygon = _isochrone_polygon(reachable, lines)
        features.append(
            feature(
                polygon,
                {
                    "kind": "isochrone",
                    "travel_type": mode.value,
                    "travel_time_min": travel_time_min,
                    "speed_kph": kmh,
                },
            )
        )

    if include in {"both", "lines"}:
        network_geom: BaseGeometry = lines
        kind = "network"
        if buffer_meters is not None and buffer_meters > 0:
            if lines.is_empty:
                raise IsochroneError("Cannot buffer an empty reachable network.")
            network_geom = _buffer_meters(lines, buffer_meters)
            kind = "network_buffer"
        elif lines.is_empty:
            raise IsochroneError("No reachable road geometries within the travel time.")
        features.append(
            feature(
                network_geom,
                {
                    "kind": kind,
                    "travel_type": mode.value,
                    "travel_time_min": travel_time_min,
                    "speed_kph": kmh,
                    "buffer_meters": buffer_meters if kind == "network_buffer" else None,
                },
            )
        )

    return feature_collection(
        features,
        metadata={
            "lat": lat,
            "lon": lon,
            "travel_time_min": travel_time_min,
            "travel_type": mode.value,
            "speed_kph": kmh,
            "include": include,
            "buffer_meters": buffer_meters,
            "origin_node": int(origin) if _is_int_like(origin) else origin,
        },
    )


def _parse_include(value: str) -> Include:
    normalized = str(value).strip().lower()
    if normalized not in {"both", "polygon", "lines"}:
        raise ValueError("include must be 'both', 'polygon', or 'lines'")
    return normalized  # type: ignore[return-value]


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


def _isochrone_polygon(graph, lines: BaseGeometry) -> BaseGeometry:
    points = [
        Point(float(data["x"]), float(data["y"]))
        for _, data in graph.nodes(data=True)
        if "x" in data and "y" in data
    ]
    if not points and not lines.is_empty:
        return _buffer_meters(lines, 25.0)
    if not points:
        raise IsochroneError("Reachable network has no coordinates to build a polygon.")
    if len(points) == 1:
        return _buffer_meters(points[0], 25.0)
    if len(points) == 2:
        return _buffer_meters(LineString(points), 25.0)

    hull = concave_hull(MultiPoint(points), ratio=0.2, allow_holes=False)
    if hull.is_empty or hull.geom_type not in {"Polygon", "MultiPolygon"}:
        hull = MultiPoint(points).convex_hull
    if hull.geom_type not in {"Polygon", "MultiPolygon"}:
        return _buffer_meters(hull, 25.0)
    return hull


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
