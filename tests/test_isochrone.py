import networkx as nx
import pytest

from oisochron import IsochroneError, isochrone_from_graph


def _grid_graph() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    # 3x3 lon/lat grid around Milan, 100 m edges, walkable in every direction.
    origin_lon, origin_lat = 9.19, 45.46
    step = 0.001
    nodes = {}
    node_id = 1
    for row in range(3):
        for col in range(3):
            lon = origin_lon + col * step
            lat = origin_lat + row * step
            graph.add_node(node_id, x=lon, y=lat)
            nodes[(row, col)] = node_id
            node_id += 1

    for row, col in nodes:
        current = nodes[(row, col)]
        for drow, dcol in ((0, 1), (1, 0)):
            neighbor = nodes.get((row + drow, col + dcol))
            if neighbor is None:
                continue
            graph.add_edge(current, neighbor, 0, length=100.0, highway="residential")
            graph.add_edge(neighbor, current, 0, length=100.0, highway="residential")
    graph.graph["crs"] = "EPSG:4326"
    return graph


def test_isochrone_returns_polygon_and_lines() -> None:
    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="both",
    )
    kinds = {feature["properties"]["kind"] for feature in result["features"]}
    assert kinds == {"isochrone", "network"}
    geometries = {feature["geometry"]["type"] for feature in result["features"]}
    assert "Polygon" in geometries or "MultiPolygon" in geometries
    assert "LineString" in geometries or "MultiLineString" in geometries


def test_include_polygon_only() -> None:
    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="polygon",
    )
    assert len(result["features"]) == 1
    assert result["features"][0]["properties"]["kind"] == "isochrone"


def test_buffer_replaces_lines() -> None:
    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="lines",
        buffer_meters=20,
    )
    assert len(result["features"]) == 1
    feature = result["features"][0]
    assert feature["properties"]["kind"] == "network_buffer"
    assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_empty_graph_raises() -> None:
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:4326"
    with pytest.raises(IsochroneError):
        isochrone_from_graph(graph, 45.46, 9.19, 10, "foot")
