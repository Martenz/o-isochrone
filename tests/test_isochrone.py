import networkx as nx
import pytest
from shapely.geometry import LineString, Point, shape
from shapely.ops import unary_union

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
    assert kinds == {"filled_buffer", "lines"}
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
    assert result["features"][0]["properties"]["kind"] == "filled_buffer"


def test_default_include_is_buffered_lines() -> None:
    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
    )
    assert [feature["properties"]["kind"] for feature in result["features"]] == ["buffered_lines"]
    assert result["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_buffer_layer() -> None:
    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="buffered_lines",
        buffer_meters=20,
    )
    assert len(result["features"]) == 1
    feature = result["features"][0]
    assert feature["properties"]["kind"] == "buffered_lines"
    assert feature["properties"]["buffer_meters"] == 20
    assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_empty_graph_raises() -> None:
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:4326"
    with pytest.raises(IsochroneError):
        isochrone_from_graph(graph, 45.46, 9.19, 10, "foot")


def test_isochrone_polygon_includes_road_vertices() -> None:
    """Hull must use edge geometry, not only intersection nodes."""
    bulge = (9.195, 45.465)
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=9.19, y=45.46)
    graph.add_node(2, x=9.20, y=45.46)
    geom = LineString([(9.19, 45.46), bulge, (9.20, 45.46)])
    graph.add_edge(1, 2, 0, length=800.0, highway="residential", geometry=geom)
    graph.add_edge(2, 1, 0, length=800.0, highway="residential", geometry=geom.reverse())
    graph.graph["crs"] = "EPSG:4326"

    result = isochrone_from_graph(
        graph,
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="filled_buffer",
    )
    polygon = shape(result["features"][0]["geometry"])
    assert polygon.covers(Point(*bulge)) or polygon.intersects(Point(*bulge).buffer(1e-8))


def test_isochrone_polygon_covers_opening_of_horseshoe() -> None:
    """A hull can cut the chord of a C-shaped network; a network buffer cannot."""
    graph = nx.MultiDiGraph()
    nodes = {
        1: (9.190, 45.462),
        2: (9.192, 45.462),
        3: (9.194, 45.462),
        4: (9.190, 45.460),
        5: (9.194, 45.460),
    }
    for node_id, (lon, lat) in nodes.items():
        graph.add_node(node_id, x=lon, y=lat)
    edges = [(1, 2), (2, 3), (1, 4), (3, 5), (4, 5)]
    for start, end in edges:
        geom = LineString([nodes[start], nodes[end]])
        graph.add_edge(start, end, 0, length=200.0, highway="residential", geometry=geom)
        graph.add_edge(end, start, 0, length=200.0, highway="residential", geometry=geom.reverse())
    graph.graph["crs"] = "EPSG:4326"

    result = isochrone_from_graph(
        graph,
        45.462,
        9.190,
        travel_time_min=10,
        travel_type="foot",
        include="both",
    )
    by_kind = {feature["properties"]["kind"]: shape(feature["geometry"]) for feature in result["features"]}
    midpoint = Point(9.192, 45.460)
    assert by_kind["filled_buffer"].intersects(midpoint.buffer(1e-6))
    assert by_kind["lines"].intersects(midpoint.buffer(1e-6))


def test_multi_include_hulls_and_h3() -> None:
    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="lines,buffered_lines,filled_buffer,convex_hull,concave_hull,h3",
        buffer_meters=15,
        h3_resolution=10,
    )
    kinds = [feature["properties"]["kind"] for feature in result["features"]]
    assert kinds[:5] == [
        "lines",
        "buffered_lines",
        "filled_buffer",
        "convex_hull",
        "concave_hull",
    ]
    h3_features = [feature for feature in result["features"] if feature["properties"]["kind"] == "h3"]
    assert h3_features
    assert all(feature["geometry"]["type"] in {"Polygon", "MultiPolygon"} for feature in h3_features)
    for feature in result["features"]:
        if feature["properties"]["kind"] == "lines":
            assert feature["geometry"]["type"] in {"LineString", "MultiLineString"}
        elif feature["properties"]["kind"] != "h3":
            assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_parse_include_aliases_and_default() -> None:
    from oisochron import parse_include

    assert parse_include(None) == ("buffered_lines",)
    assert parse_include("") == ("buffered_lines",)
    assert parse_include("both") == ("lines", "filled_buffer")
    assert parse_include(["convex_hull", "lines", "lines"]) == ("convex_hull", "lines")
    with pytest.raises(ValueError, match="unknown layer"):
        parse_include("not-a-layer")


def test_h3_includes_intermediate_cells_along_road() -> None:
    """Every hex the road passes through must appear, not only cells near centroids."""
    start = (8.72259, 45.84223)
    end = (8.74259, 45.84223)
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=start[0], y=start[1])
    graph.add_node(2, x=end[0], y=end[1])
    geom = LineString([start, end])
    graph.add_edge(1, 2, 0, length=1600.0, highway="residential", geometry=geom)
    graph.add_edge(2, 1, 0, length=1600.0, highway="residential", geometry=geom.reverse())
    graph.graph["crs"] = "EPSG:4326"

    result = isochrone_from_graph(
        graph,
        start[1],
        start[0],
        travel_time_min=30,
        travel_type="foot",
        include="h3",
        h3_resolution=10,
    )
    h3_features = [item for item in result["features"] if item["properties"]["kind"] == "h3"]
    assert len(h3_features) >= 8
    hexes = unary_union([shape(item["geometry"]) for item in h3_features])
    samples = [geom.interpolate(t, normalized=True) for t in (0.0, 0.25, 0.5, 0.75, 1.0)]
    for point in samples:
        assert hexes.covers(point) or hexes.intersects(point.buffer(1e-9))
    distances = [item["properties"]["route_distance_m"] for item in h3_features]
    assert min(distances) == 0
    assert max(distances) > min(distances)
    assert {item["properties"]["progress"] for item in h3_features} >= {0.0, 1.0}
