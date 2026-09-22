"""o-isochrone — OSM road-network isochrones as GeoJSON."""

from oisochrone.config import Settings, get_settings
from oisochrone.exceptions import IsochroneError
from oisochrone.isochrone import (
    BatchOriginResult,
    IncludeLayer,
    IsochroneOrigin,
    aiter_isochrones,
    assign_origin_ids,
    compute_isochrone,
    compute_isochrones,
    compute_isochrones_async,
    isochrone_from_graph,
    iter_isochrones,
    parse_include,
)
from oisochrone.network import network_extent_geojson
from oisochrone.speeds import TravelType, parse_travel_type, speed_kph

__version__ = "0.1.0"

get_network_extent = network_extent_geojson

__all__ = [
    "BatchOriginResult",
    "IncludeLayer",
    "IsochroneError",
    "IsochroneOrigin",
    "Settings",
    "TravelType",
    "aiter_isochrones",
    "assign_origin_ids",
    "compute_isochrone",
    "compute_isochrones",
    "compute_isochrones_async",
    "get_network_extent",
    "get_settings",
    "isochrone_from_graph",
    "iter_isochrones",
    "parse_include",
    "parse_travel_type",
    "speed_kph",
    "__version__",
]
