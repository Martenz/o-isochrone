"""Tests for metric edge lengths and travel-time assignment."""

import math

import networkx as nx
import pytest
from shapely.geometry import LineString, MultiLineString, Point

from oisochrone.costs import (
    assign_travel_times,
    coordinates_length_m,
    edge_length_m,
    geometry_length_m,
    great_circle_m,
    node_pair_length_m,
)

MILAN_LAT = 45.46
FOOT_MPS = 5.0 / 3.6


def _graph_with_edge(**edge_data) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=9.19, y=MILAN_LAT)
    graph.add_node(2, x=9.191, y=MILAN_LAT)
    graph.add_edge(1, 2, 0, highway="residential", **edge_data)
    graph.graph["crs"] = "EPSG:4326"
    return graph


def _ew_geometry() -> LineString:
    return LineString([(9.19, MILAN_LAT), (9.191, MILAN_LAT)])


def test_great_circle_matches_known_distances() -> None:
    assert great_circle_m(9.19, 45.0, 9.19, 45.0) == 0.0
    # one degree of latitude is ~111.2 km
    assert great_circle_m(9.19, 45.0, 9.19, 46.0) == pytest.approx(111_195, rel=0.002)


def test_east_west_length_is_latitude_correct() -> None:
    """0.001 deg of longitude at 45.46 N is ~78 m, not the flat 111 m."""
    east_west = coordinates_length_m([(9.190, MILAN_LAT), (9.191, MILAN_LAT)])
    north_south = coordinates_length_m([(9.190, MILAN_LAT), (9.190, MILAN_LAT + 0.001)])
    assert east_west is not None and north_south is not None
    assert east_west == pytest.approx(77.99, abs=0.5)
    assert north_south == pytest.approx(111.19, abs=0.5)
    # the flat approximation both helpers used before was 1 / cos(lat) too long
    assert east_west < 111.32 / 1.3


def test_geometry_length_handles_parts_and_rejects_non_lines() -> None:
    part = LineString([(9.19, MILAN_LAT), (9.191, MILAN_LAT)])
    single = coordinates_length_m(part.coords)
    assert single is not None
    multipart = MultiLineString([part, LineString([(9.20, MILAN_LAT), (9.201, MILAN_LAT)])])
    assert geometry_length_m(multipart) == pytest.approx(2 * single)
    assert geometry_length_m(None) is None
    assert geometry_length_m(LineString()) is None
    assert geometry_length_m(Point(9.19, MILAN_LAT)) is None
    assert coordinates_length_m([(9.19, MILAN_LAT)]) is None


def test_node_pair_length_requires_coordinates() -> None:
    graph = _graph_with_edge()
    assert node_pair_length_m(graph, 1, 2) == pytest.approx(77.99, abs=0.5)
    graph.add_node(3)
    assert node_pair_length_m(graph, 1, 3) is None


def test_osm_length_wins_over_geometry() -> None:
    """An advertised OSM length is authoritative even if the geometry is shorter."""
    graph = _graph_with_edge(length=250.0, geometry=_ew_geometry())
    edge = graph.edges[1, 2, 0]
    assert edge_length_m(graph, 1, 2, edge) == 250.0
    assign_travel_times(graph, 5.0)
    assert edge["length"] == 250.0
    assert edge["travel_time"] == pytest.approx(250.0 / FOOT_MPS)


def test_zero_length_is_treated_as_missing() -> None:
    graph = _graph_with_edge(length=0.0, geometry=_ew_geometry())
    assign_travel_times(graph, 5.0)
    assert graph.edges[1, 2, 0]["length"] == pytest.approx(77.99, abs=0.5)


def test_length_is_derived_from_geometry() -> None:
    graph = _graph_with_edge(geometry=_ew_geometry())
    assign_travel_times(graph, 5.0)
    edge = graph.edges[1, 2, 0]
    assert edge["length"] == pytest.approx(77.99, abs=0.5)
    assert edge["travel_time"] == pytest.approx(77.99 / FOOT_MPS, rel=0.01)


def test_length_falls_back_to_node_coordinates() -> None:
    """A third of a real OSMnx graph has no edge geometry; nodes still measure it."""
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=9.19, y=MILAN_LAT)
    graph.add_node(2, x=9.20, y=MILAN_LAT)
    graph.add_edge(1, 2, 0, highway="residential")
    assign_travel_times(graph, 5.0)
    edge = graph.edges[1, 2, 0]
    assert edge["length"] == pytest.approx(779.9, abs=1.0)
    assert edge["travel_time"] == pytest.approx(779.9 / FOOT_MPS, rel=0.01)
    assert math.isfinite(edge["travel_time"])


def test_unmeasurable_edge_stays_infinite() -> None:
    graph = nx.MultiDiGraph()
    graph.add_node(1)
    graph.add_node(2)
    graph.add_edge(1, 2, 0, highway="residential")
    assign_travel_times(graph, 5.0)
    edge = graph.edges[1, 2, 0]
    assert edge["travel_time"] == float("inf")
    assert "length" not in edge


def test_length_is_backfilled_for_length_weighted_traversal() -> None:
    """The H3 layer traverses on weight="length"; networkx silently treats a
    missing attribute as 1, which turned meters into hop counts."""
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=9.19, y=MILAN_LAT)
    graph.add_node(2, x=9.20, y=MILAN_LAT)
    graph.add_edge(1, 2, 0, highway="residential")
    assign_travel_times(graph, 5.0)
    distances = nx.single_source_dijkstra_path_length(graph, 1, weight="length")
    assert distances[2] == pytest.approx(779.9, abs=1.0)


@pytest.mark.parametrize("speed", [0, -5.0, float("nan")])
def test_invalid_speed_rejected(speed: float) -> None:
    graph = _graph_with_edge(length=10.0)
    with pytest.raises(ValueError, match="greater than 0"):
        assign_travel_times(graph, speed)


def test_speed_must_be_numeric() -> None:
    graph = _graph_with_edge(length=10.0)
    with pytest.raises(ValueError, match="must be a number"):
        assign_travel_times(graph, "fast")  # type: ignore[arg-type]


def test_travel_time_tracks_speed() -> None:
    graph = _graph_with_edge(length=1000.0)
    assign_travel_times(graph, 5.0)
    foot = graph.edges[1, 2, 0]["travel_time"]
    graph_cycle = _graph_with_edge(length=1000.0)
    assign_travel_times(graph_cycle, 15.0)
    bicycle = graph_cycle.edges[1, 2, 0]["travel_time"]
    assert foot == pytest.approx(720.0)
    assert bicycle == pytest.approx(240.0)
