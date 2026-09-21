"""Download or clip a portion of the OSM road network with OSMnx."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import networkx as nx
import osmnx as ox
from shapely.geometry import box

from oisochron.config import Settings, get_settings
from oisochron.exceptions import IsochroneError
from oisochron.geojson import feature
from oisochron.speeds import NETWORK_TYPE, TravelType, highway_allowed, parse_travel_type

logger = logging.getLogger(__name__)


def configure_osmnx(settings: Settings | None = None) -> None:
    """Apply shared Overpass / cache settings to OSMnx."""
    settings = settings or get_settings()
    settings.cache_folder.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = settings.use_cache
    ox.settings.cache_folder = str(settings.cache_folder)
    ox.settings.timeout = settings.overpass_timeout
    # OSMnx expects the Overpass API root (it appends /interpreter).
    ox.settings.overpass_url = settings.overpass_url


def load_road_network(
    lat: float,
    lon: float,
    dist_m: float,
    travel_type: str | TravelType,
    settings: Settings | None = None,
):
    """Return an OSMnx MultiDiGraph around a point.

    Uses a local highway-only PBF when `OSM_SOURCE=pbf` and the file exists,
    otherwise downloads the portion from Overpass through OSMnx.
    """
    settings = settings or get_settings()
    configure_osmnx(settings)
    mode = parse_travel_type(travel_type)

    if settings.osm_source == "pbf" and not settings.osm_pbf_path.is_file():
        logger.warning(
            "OSM_SOURCE=pbf but %s is missing; falling back to Overpass.",
            settings.osm_pbf_path,
        )

    if settings.use_pbf:
        return _load_from_pbf(lat, lon, dist_m, mode, settings)
    return _load_from_overpass(lat, lon, dist_m, mode)


def network_extent_geojson(settings: Settings | None = None) -> dict:
    """Polygon covering the configured / downloaded road-network extract."""
    settings = settings or get_settings()
    if settings.osm_extent_path.is_file():
        payload = json.loads(settings.osm_extent_path.read_text(encoding="utf-8"))
        if payload.get("type") == "FeatureCollection" and payload.get("features"):
            first = payload["features"][0]
            first.setdefault("properties", {})
            first["properties"].setdefault("kind", "network_extent")
            return first
        if payload.get("type") == "Feature":
            payload.setdefault("properties", {})
            payload["properties"].setdefault("kind", "network_extent")
            return payload

    west, south, east, north = settings.osm_bbox
    return feature(
        box(west, south, east, north),
        {
            "kind": "network_extent",
            "source": settings.osm_source,
            "pbf": str(settings.osm_pbf_path) if settings.use_pbf else None,
        },
    )


def write_extent_from_bbox(
    bbox: tuple[float, float, float, float],
    path: Path,
    properties: dict | None = None,
) -> None:
    """Write a GeoJSON polygon for west,south,east,north."""
    west, south, east, north = bbox
    payload = feature(
        box(west, south, east, north),
        {"kind": "network_extent", **(properties or {})},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_from_overpass(lat: float, lon: float, dist_m: float, mode: TravelType):
    try:
        return ox.graph_from_point(
            (lat, lon),
            dist=float(dist_m),
            dist_type="bbox",
            network_type=NETWORK_TYPE[mode],
            simplify=True,
        )
    except Exception as exc:  # osmnx raises EmptyOverpassResponse and others
        raise IsochroneError(
            f"Could not download a road network around ({lat}, {lon}): {exc}"
        ) from exc


def _load_from_pbf(
    lat: float,
    lon: float,
    dist_m: float,
    mode: TravelType,
    settings: Settings,
):
    if shutil.which("osmium") is None:
        raise IsochroneError(
            "OSM_SOURCE=pbf requires the osmium CLI (osmium-tool) on PATH. "
            "Install it or set OSM_SOURCE=overpass."
        )

    west, south, east, north = _bbox_from_point(lat, lon, dist_m)
    settings.data_dir.joinpath("tmp").mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=settings.data_dir / "tmp") as tmp:
        extract_pbf = Path(tmp) / "extract.osm.pbf"
        extract_xml = Path(tmp) / "extract.osm"
        bbox = f"{west},{south},{east},{north}"
        try:
            subprocess.run(
                [
                    "osmium",
                    "extract",
                    "-b",
                    bbox,
                    "--strategy",
                    "complete_ways",
                    str(settings.osm_pbf_path),
                    "-o",
                    str(extract_pbf),
                    "--overwrite",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "osmium",
                    "cat",
                    str(extract_pbf),
                    "-f",
                    "osm",
                    "-o",
                    str(extract_xml),
                    "--overwrite",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise IsochroneError(f"osmium failed to clip the OSM extract: {detail}") from exc

        if extract_xml.stat().st_size < 200:
            raise IsochroneError(
                f"No OSM roads found in the clip around ({lat}, {lon})."
            )

        try:
            graph = ox.graph_from_xml(
                extract_xml,
                bidirectional=mode != TravelType.car,
                simplify=True,
                retain_all=False,
            )
        except Exception as exc:
            raise IsochroneError(
                f"Could not build a road graph from the local PBF clip: {exc}"
            ) from exc

    return _filter_graph_for_mode(graph, mode)


def _filter_graph_for_mode(graph, mode: TravelType):
    remove = [
        (u, v, k)
        for u, v, k, data in graph.edges(keys=True, data=True)
        if not highway_allowed(data.get("highway"), mode)
    ]
    graph.remove_edges_from(remove)
    graph.remove_nodes_from(list(nx.isolates(graph)))
    if graph.number_of_edges() == 0:
        raise IsochroneError(
            f"Local extract has no {mode.value} roads in the requested area."
        )
    return graph


def _bbox_from_point(lat: float, lon: float, dist_m: float) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) for a distance in meters."""
    bbox = ox.utils_geo.bbox_from_point((lat, lon), dist=float(dist_m))
    if hasattr(bbox, "west"):
        return float(bbox.west), float(bbox.south), float(bbox.east), float(bbox.north)
    west, south, east, north = bbox
    return float(west), float(south), float(east), float(north)
