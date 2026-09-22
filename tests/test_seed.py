"""Tests for coordinate seed loading and tile seeding."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import networkx as nx

from oisochrone.config import Settings, clear_settings_cache
from oisochrone.seed import expand_seed_jobs, load_coordinate_seed, seed_graph_tiles
from oisochrone.speeds import TravelType


def test_expand_seed_jobs_caps_car() -> None:
    jobs = expand_seed_jobs(
        ("foot", "bicycle", "car"),
        (15.0, 30.0),
        car_max_travel_time_min=15.0,
    )
    assert jobs == [
        (TravelType.foot, 15.0),
        (TravelType.foot, 30.0),
        (TravelType.bicycle, 15.0),
        (TravelType.bicycle, 30.0),
        (TravelType.car, 15.0),
    ]


def _tiny_graph() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=9.19, y=45.46)
    graph.add_node(2, x=9.20, y=45.46)
    graph.add_edge(1, 2, 0, length=100.0, highway="residential")
    graph.graph["crs"] = "EPSG:4326"
    return graph


def _settings(tmp_path: Path, seed_path: Path, **overrides) -> Settings:
    cache_dir = tmp_path / "graph_cache"
    base = dict(
        data_dir=tmp_path,
        osm_source="overpass",
        osm_pbf_url="https://example.invalid/italy.osm.pbf",
        osm_pbf_path=tmp_path / "missing.osm.pbf",
        osm_extent_path=tmp_path / "extent.geojson",
        osm_bbox=(6.0, 35.0, 19.0, 48.0),
        overpass_url="https://example.invalid/api",
        cache_folder=tmp_path / "osmnx_cache",
        use_cache=False,
        overpass_timeout=30,
        speed_foot_kph=5.0,
        speed_bicycle_kph=15.0,
        speed_car_kph=40.0,
        download_margin=1.2,
        max_travel_time_min=120.0,
        graph_cache_enabled=True,
        graph_cache_dir=cache_dir,
        graph_cache_memory_size=8,
        graph_cache_grid_deg=0.05,
        graph_sqlite_path=cache_dir / "graph_tiles.sqlite",
        graph_preload=False,
        graph_seed_on_startup=False,
        graph_seed_coordinates_path=seed_path,
        graph_seed_modes=("foot", "bicycle", "car"),
        graph_seed_travel_times_min=(15.0, 30.0),
        graph_seed_car_max_travel_time_min=15.0,
    )
    base.update(overrides)
    return Settings(**base)


def test_bundled_coordinate_seed_has_italy_capitals() -> None:
    points = load_coordinate_seed()
    assert len(points) == 107
    milan = next(p for p in points if p.name == "Milano")
    assert abs(milan.lat - 45.4642) < 1e-6


def test_load_coordinate_seed_wrapper_and_list(tmp_path: Path) -> None:
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps(
            {
                "description": "test",
                "points": [{"id": "a", "name": "A", "lat": 45.0, "lon": 9.0}],
            }
        ),
        encoding="utf-8",
    )
    bare = tmp_path / "bare.json"
    bare.write_text(
        json.dumps([{"lat": 44.0, "longitude": 8.0, "name": "B"}]),
        encoding="utf-8",
    )
    assert load_coordinate_seed(wrapped)[0].id == "a"
    assert load_coordinate_seed(bare)[0].lon == 8.0


def test_seed_graph_tiles_builds_then_skips(tmp_path: Path) -> None:
    clear_settings_cache()
    seed_path = tmp_path / "coords.json"
    seed_path.write_text(
        json.dumps(
            {
                "points": [
                    {"id": "m", "name": "Milan", "lat": 45.46, "lon": 9.19},
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = _settings(
        tmp_path,
        seed_path,
        graph_seed_modes=("foot",),
        graph_seed_travel_times_min=(15.0,),
    )
    built_graph = _tiny_graph()

    with patch("oisochrone.network._load_from_overpass", return_value=built_graph) as builder:
        with patch("oisochrone.network.snap_clip", return_value=(45.45, 9.20, 2000.0)):
            with patch("oisochrone.seed.snap_clip", return_value=(45.45, 9.20, 2000.0)):
                with patch(
                    "oisochrone.network._bbox_from_point",
                    return_value=(9.11, 45.41, 9.19, 45.49),
                ):
                    first = seed_graph_tiles(settings)
                    second = seed_graph_tiles(settings)

    assert first.total == 1
    assert first.built == 1
    assert first.skipped == 0
    assert second.built == 0
    assert second.skipped == 1
    assert builder.call_count == 1
