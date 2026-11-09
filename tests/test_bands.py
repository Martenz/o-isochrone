"""Tests for nested isochrone bands."""

from unittest.mock import patch

import networkx as nx
import pytest
from shapely.geometry import shape

from oisochrone import isochrone_bands, isochrone_from_graph


def _grid_graph(rows: int = 9, cols: int = 9, step: float = 0.002) -> nx.MultiDiGraph:
    """Lon/lat grid around Milan with 150-200 m edges and explicit lengths."""
    graph = nx.MultiDiGraph()
    origin_lon, origin_lat = 9.17, 45.45
    nodes: dict[tuple[int, int], int] = {}
    node_id = 1
    for row in range(rows):
        for col in range(cols):
            graph.add_node(node_id, x=origin_lon + col * step, y=origin_lat + row * step)
            nodes[(row, col)] = node_id
            node_id += 1
    for row, col in nodes:
        current = nodes[(row, col)]
        for drow, dcol in ((0, 1), (1, 0)):
            neighbor = nodes.get((row + drow, col + dcol))
            if neighbor is None:
                continue
            graph.add_edge(current, neighbor, 0, length=150.0, highway="residential")
            graph.add_edge(neighbor, current, 0, length=150.0, highway="residential")
    graph.graph["crs"] = "EPSG:4326"
    return graph


def _area(collection: dict) -> float:
    return sum(shape(feature["geometry"]).area for feature in collection["features"])


def test_bands_are_ordered_nested_and_metadata_tagged() -> None:
    graph = _grid_graph()
    bands = isochrone_bands(
        graph,
        45.45,
        9.17,
        [20, 5, 10],
        "foot",
        include="filled_buffer",
        origin_id="duomo",
    )
    assert len(bands) == 3
    times = [band["metadata"]["travel_time_min"] for band in bands]
    assert times == [5.0, 10.0, 20.0]
    assert [band["metadata"]["band_index"] for band in bands] == [0, 1, 2]
    assert [band["metadata"]["band_count"] for band in bands] == [3, 3, 3]
    assert all(band["metadata"]["origin_id"] == "duomo" for band in bands)
    assert all(band["metadata"]["travel_type"] == "foot" for band in bands)

    areas = [_area(band) for band in bands]
    assert areas == sorted(areas)
    assert areas[0] < areas[-1]

    polygons = [shape(band["features"][0]["geometry"]) for band in bands]
    for inner, outer in zip(polygons, polygons[1:]):
        assert inner.difference(outer.buffer(1e-12)).area < inner.area * 1e-6


def test_bands_carry_band_properties_on_features() -> None:
    bands = isochrone_bands(_grid_graph(), 45.45, 9.17, [5, 10], "foot", include="buffered_lines")
    for index, band in enumerate(bands):
        props = band["features"][0]["properties"]
        assert props["band_index"] == index
        assert props["band_count"] == 2
        assert props["travel_time_min"] == band["metadata"]["travel_time_min"]


def test_bands_dedupe_and_sort_travel_times() -> None:
    bands = isochrone_bands(_grid_graph(), 45.45, 9.17, [20, 5, 5, 5.0], "foot")
    assert [band["metadata"]["travel_time_min"] for band in bands] == [5.0, 20.0]
    assert [band["metadata"]["band_count"] for band in bands] == [2, 2]


def test_single_band_matches_single_isochrone() -> None:
    graph = _grid_graph()
    band = isochrone_bands(graph, 45.45, 9.17, [10], "foot", include="both", origin_id="a")[0]
    single = isochrone_from_graph(graph, 45.45, 9.17, 10, "foot", include="both", origin_id="a")

    def _strip(collection: dict) -> dict:
        cleaned = {"type": collection["type"], "features": []}
        for feature in collection["features"]:
            properties = {
                key: value
                for key, value in feature["properties"].items()
                if key not in {"band_index", "band_count"}
            }
            cleaned["features"].append({**feature, "properties": properties})
        return cleaned

    assert _strip(band) == _strip(single)
    assert band["features"][0]["properties"]["band_index"] == 0
    metadata = dict(band["metadata"])
    assert metadata.pop("band_index") == 0
    assert metadata.pop("band_count") == 1
    assert metadata == single["metadata"]


def test_bands_share_one_shortest_path_search() -> None:
    graph = _grid_graph()
    with patch(
        "oisochrone.isochrone.nx.single_source_dijkstra_path_length",
        wraps=nx.single_source_dijkstra_path_length,
    ) as dijkstra:
        isochrone_bands(graph, 45.45, 9.17, [2, 4, 6, 8], "foot")
    assert dijkstra.call_count == 1
    assert dijkstra.call_args.kwargs["cutoff"] == 8 * 60


@pytest.mark.parametrize(
    "travel_times",
    [[], [0], [-5], ["ten"], [True], None],
)
def test_bands_reject_invalid_travel_times(travel_times) -> None:
    with pytest.raises(ValueError):
        isochrone_bands(_grid_graph(), 45.45, 9.17, travel_times, "foot")


def test_bands_accept_a_scalar_travel_time() -> None:
    bands = isochrone_bands(_grid_graph(), 45.45, 9.17, 7, "foot")
    assert [band["metadata"]["travel_time_min"] for band in bands] == [7.0]


def test_bands_unknown_travel_type_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown travel type"):
        isochrone_bands(_grid_graph(), 45.45, 9.17, [5], "submarine")


def test_bands_reject_empty_graph() -> None:
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:4326"
    with pytest.raises(Exception, match="empty"):
        isochrone_bands(graph, 45.45, 9.17, [5], "foot")
