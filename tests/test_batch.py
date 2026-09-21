import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from oisochron import IsochroneOrigin, assign_origin_ids, compute_isochrones, compute_isochrones_async
from oisochron.isochrone import coerce_origin


def test_assign_origin_ids_fills_incremental_gaps() -> None:
    assigned = assign_origin_ids(
        [
            IsochroneOrigin(45.46, 9.19),
            {"lat": 45.47, "lon": 9.20, "id": "1"},
            IsochroneOrigin(45.48, 9.21, id="centrale"),
        ]
    )
    assert [origin.id for origin in assigned] == ["2", "1", "centrale"]


def test_assign_origin_ids_rejects_duplicates_and_empty() -> None:
    with pytest.raises(ValueError, match="duplicate origin id"):
        assign_origin_ids(
            [
                IsochroneOrigin(45.46, 9.19, id="a"),
                IsochroneOrigin(45.47, 9.20, id="a"),
            ]
        )
    with pytest.raises(ValueError, match="at least one"):
        assign_origin_ids([])


def test_coerce_origin_accepts_mapping_aliases_and_objects() -> None:
    from_dict = coerce_origin({"latitude": 45.46, "lng": 9.19, "origin_id": "duomo"})
    assert from_dict == IsochroneOrigin(45.46, 9.19, id="duomo")
    from_obj = coerce_origin(SimpleNamespace(lat=45.47, lon=9.20, id="2"))
    assert from_obj.id == "2"
    assert from_obj.lat == 45.47


def _fake_collection(lat, lon, *args, origin_id=None, **kwargs):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "buffered_lines", "origin_id": origin_id},
                "geometry": None,
            }
        ],
        "metadata": {"lat": lat, "lon": lon, "origin_id": origin_id},
    }


@patch("oisochron.isochrone.compute_isochrone", side_effect=_fake_collection)
def test_compute_isochrones_stamps_ids_and_preserves_order(mock_compute) -> None:
    results = compute_isochrones(
        [
            IsochroneOrigin(45.46, 9.19),
            {"lat": 45.47, "lon": 9.20, "id": "centrale"},
            IsochroneOrigin(45.48, 9.21),
        ],
        travel_time_min=10,
        travel_type="foot",
        include="buffered_lines",
        max_concurrency=2,
    )
    assert [item["metadata"]["origin_id"] for item in results] == ["1", "centrale", "2"]
    assert mock_compute.call_count == 3
    first_kwargs = mock_compute.call_args_list[0].kwargs
    assert first_kwargs["origin_id"] == "1"
    assert mock_compute.call_args_list[1].kwargs["origin_id"] == "centrale"


@patch("oisochron.isochrone.compute_isochrone", side_effect=_fake_collection)
def test_compute_isochrones_async_parallel(mock_compute) -> None:
    results = asyncio.run(
        compute_isochrones_async(
            [
                IsochroneOrigin(45.4642, 9.19, id="duomo"),
                IsochroneOrigin(45.4863, 9.2045),
                IsochroneOrigin(45.4515, 9.175),
            ],
            travel_time_min=12,
            travel_type="bicycle",
            max_concurrency=3,
        )
    )
    assert [item["metadata"]["origin_id"] for item in results] == ["duomo", "1", "2"]
    assert mock_compute.call_count == 3


def test_isochrone_from_graph_stamps_origin_id() -> None:
    from tests.test_isochrone import _grid_graph
    from oisochron import isochrone_from_graph

    result = isochrone_from_graph(
        _grid_graph(),
        45.46,
        9.19,
        travel_time_min=10,
        travel_type="foot",
        include="buffered_lines",
        origin_id="duomo",
    )
    assert result["metadata"]["origin_id"] == "duomo"
    assert all(feature["properties"]["origin_id"] == "duomo" for feature in result["features"])
