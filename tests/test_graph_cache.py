"""Tests for warm + SQLite graph tile cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import networkx as nx
import pytest

from oisochrone.config import Settings, clear_settings_cache
from oisochrone.graph_cache import (
    GraphCache,
    cache_key,
    preload_graph_cache,
    quantize_bbox,
    reset_graph_cache,
    snap_clip,
    source_fingerprint,
)
from oisochrone.network import load_road_network
from oisochrone.speeds import TravelType


def _tiny_graph() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=9.19, y=45.46)
    graph.add_node(2, x=9.20, y=45.46)
    graph.add_edge(1, 2, 0, length=100.0, highway="residential", travel_time=72.0)
    graph.graph["crs"] = "EPSG:4326"
    return graph


def _settings(tmp_path: Path, **overrides) -> Settings:
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
        graph_cache_memory_size=4,
        graph_cache_grid_deg=0.05,
        graph_sqlite_path=cache_dir / "graph_tiles.sqlite",
        graph_preload=True,
        graph_seed_on_startup=False,
        graph_seed_coordinates_path=tmp_path / "coordinate_seed.json",
        graph_seed_modes=("foot", "bicycle", "car"),
        graph_seed_travel_times_min=(15.0, 30.0),
        graph_seed_car_max_travel_time_min=15.0,
    )
    base.update(overrides)
    return Settings(**base)


def test_quantize_bbox_expands_outward() -> None:
    assert quantize_bbox((9.11, 45.41, 9.19, 45.49), 0.05) == pytest.approx(
        (9.10, 45.40, 9.20, 45.50)
    )


def test_snap_clip_keeps_radius_near_travel_distance() -> None:
    lat_s, lon_s, dist_s = snap_clip(45.43856, 10.99282, 1500.0, 0.05)
    assert 1500 <= dist_s <= 3500
    bike = snap_clip(45.84223, 8.72259, 4500.0, 0.05)[2]
    car = snap_clip(45.84223, 8.72259, 12000.0, 0.05)[2]
    assert bike <= 6000
    assert 12000 <= car <= 14000


def test_graph_cache_memory_and_sqlite_roundtrip(tmp_path: Path) -> None:
    cache_dir = tmp_path / "graphs"
    cache = GraphCache(
        enabled=True,
        cache_dir=cache_dir,
        sqlite_path=cache_dir / "graph_tiles.sqlite",
        memory_size=2,
        grid_deg=0.05,
        preload=True,
    )
    key = cache_key("foot", 45.45, 11.0, 2000.0, "overpass:test", speed_kph=5.0)
    graph = _tiny_graph()
    cache.put(
        key,
        graph,
        meta={"mode": "foot", "bbox": (10.9, 45.4, 11.1, 45.5), "fingerprint": "overpass:test"},
    )

    assert cache.get(key) is graph
    assert cache.stats.memory_hits == 1

    cache.clear_memory()
    restored = cache.get(key)
    assert restored is not None
    assert restored.number_of_edges() == 1
    assert cache.stats.disk_hits == 1
    assert cache.sqlite_path.is_file()
    cache.close()


def test_preload_loads_sqlite_tiles_into_memory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "graphs"
    writer = GraphCache(
        enabled=True,
        cache_dir=cache_dir,
        sqlite_path=cache_dir / "graph_tiles.sqlite",
        memory_size=2,
        grid_deg=0.05,
        preload=True,
    )
    fingerprint = "overpass:test"
    key = cache_key("foot", 45.45, 11.0, 2000.0, fingerprint, speed_kph=5.0)
    writer.put(
        key,
        _tiny_graph(),
        meta={"mode": "foot", "bbox": (10.9, 45.4, 11.1, 45.5), "fingerprint": fingerprint},
    )
    writer.close()

    reader = GraphCache(
        enabled=True,
        cache_dir=cache_dir,
        sqlite_path=cache_dir / "graph_tiles.sqlite",
        memory_size=2,
        grid_deg=0.05,
        preload=True,
    )
    assert reader.preload(fingerprint) == 1
    assert reader.get(key) is not None
    assert reader.stats.memory_hits == 1
    reader.close()


def test_load_road_network_uses_sqlite_cache(tmp_path: Path) -> None:
    clear_settings_cache()
    settings = _settings(tmp_path)
    built = _tiny_graph()
    builder = MagicMock(return_value=built)

    with patch("oisochrone.network._load_from_overpass", builder):
        with patch("oisochrone.network.snap_clip", return_value=(45.45, 9.20, 2000.0)):
            with patch(
                "oisochrone.network._bbox_from_point",
                return_value=(9.11, 45.41, 9.19, 45.49),
            ):
                first = load_road_network(45.46, 9.19, 1000, TravelType.foot, settings)
                second = load_road_network(45.46, 9.19, 1000, TravelType.foot, settings)

    assert first is built
    assert second is built
    assert builder.call_count == 1
    assert "travel_time" in list(first.edges(keys=True, data=True))[0][3]

    reset_graph_cache()
    clear_settings_cache()
    settings = _settings(tmp_path)
    loaded = preload_graph_cache(settings)
    assert loaded == 1

    builder_again = MagicMock(side_effect=AssertionError("should not rebuild"))
    with patch("oisochrone.network._load_from_overpass", builder_again):
        with patch("oisochrone.network.snap_clip", return_value=(45.45, 9.20, 2000.0)):
            with patch(
                "oisochrone.network._bbox_from_point",
                return_value=(9.11, 45.41, 9.19, 45.49),
            ):
                third = load_road_network(45.46, 9.19, 1000, TravelType.foot, settings)

    assert third.number_of_edges() == 1


def test_graph_cache_cover_hit_reuses_larger_tile(tmp_path: Path) -> None:
    cache_dir = tmp_path / "graphs"
    cache = GraphCache(
        enabled=True,
        cache_dir=cache_dir,
        sqlite_path=cache_dir / "graph_tiles.sqlite",
        memory_size=4,
        grid_deg=0.05,
        preload=True,
    )
    fingerprint = "overpass:test"
    large_key = cache_key("foot", 45.45, 11.0, 4000.0, fingerprint, speed_kph=5.0)
    cache.put(
        large_key,
        _tiny_graph(),
        meta={
            "mode": "foot",
            "bbox": (10.9, 45.4, 11.1, 45.5),
            "fingerprint": fingerprint,
            "lat_s": 45.45,
            "lon_s": 11.0,
            "dist_s": 4000.0,
            "speed_kph": 5.0,
        },
    )
    cache.clear_memory()
    covered = cache.get_covering(
        mode="foot",
        lat_s=45.45,
        lon_s=11.0,
        dist_s=2500.0,
        fingerprint=fingerprint,
        speed_kph=5.0,
    )
    assert covered is not None
    assert cache.stats.cover_hits == 1
    assert cache.covers(
        mode="foot",
        lat_s=45.45,
        lon_s=11.0,
        dist_s=2500.0,
        fingerprint=fingerprint,
        speed_kph=5.0,
    )
    cache.close()


def test_graph_cache_migrates_pre_snap_sqlite_schema(tmp_path: Path) -> None:
    """Old DBs lack lat_s/lon_s/dist_s/speed_kph; migrate before cover index."""
    import sqlite3

    cache_dir = tmp_path / "graphs"
    cache_dir.mkdir(parents=True)
    db_path = cache_dir / "graph_tiles.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE graph_tiles (
            cache_key TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            west REAL NOT NULL,
            south REAL NOT NULL,
            east REAL NOT NULL,
            north REAL NOT NULL,
            fingerprint TEXT NOT NULL,
            format_version INTEGER NOT NULL,
            graph_blob BLOB NOT NULL,
            created_at REAL NOT NULL,
            accessed_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    cache = GraphCache(
        enabled=True,
        cache_dir=cache_dir,
        sqlite_path=db_path,
        memory_size=2,
        grid_deg=0.05,
        preload=True,
    )
    cols = {
        row[1]
        for row in cache._ensure_db().execute("PRAGMA table_info(graph_tiles)")
    }
    assert {"lat_s", "lon_s", "dist_s", "speed_kph"} <= cols
    # Cover lookup must not raise "no such column: lat_s".
    assert (
        cache.get_covering(
            mode="foot",
            lat_s=45.45,
            lon_s=11.0,
            dist_s=2000.0,
            fingerprint="overpass:test",
            speed_kph=5.0,
        )
        is None
    )
    cache.close()


def test_source_fingerprint_uses_basename(tmp_path: Path) -> None:
    pbf = tmp_path / "roads.osm.pbf"
    pbf.write_bytes(b"fake")
    settings = _settings(tmp_path, osm_source="pbf", osm_pbf_path=pbf)
    token = source_fingerprint(settings)
    assert token.startswith("pbf:roads.osm.pbf:")
    assert str(tmp_path) not in token
