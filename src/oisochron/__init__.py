"""oisochron — OSM road-network isochrones as GeoJSON."""

from oisochron.config import Settings, get_settings
from oisochron.exceptions import IsochroneError
from oisochron.isochrone import compute_isochrone, isochrone_from_graph
from oisochron.network import network_extent_geojson
from oisochron.speeds import TravelType, parse_travel_type, speed_kph

__version__ = "0.1.0"

get_network_extent = network_extent_geojson

__all__ = [
    "IsochroneError",
    "Settings",
    "TravelType",
    "compute_isochrone",
    "get_network_extent",
    "get_settings",
    "isochrone_from_graph",
    "parse_travel_type",
    "speed_kph",
    "__version__",
]
