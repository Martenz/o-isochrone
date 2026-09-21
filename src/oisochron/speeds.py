"""Travel modes and the average speeds used to turn time into distance."""

from __future__ import annotations

from enum import Enum

from oisochron.config import Settings


class TravelType(str, Enum):
    foot = "foot"
    car = "car"
    bicycle = "bicycle"


_ALIASES: dict[str, TravelType] = {
    "foot": TravelType.foot,
    "feet": TravelType.foot,
    "walk": TravelType.foot,
    "walking": TravelType.foot,
    "pedestrian": TravelType.foot,
    "car": TravelType.car,
    "drive": TravelType.car,
    "driving": TravelType.car,
    "auto": TravelType.car,
    "bicycle": TravelType.bicycle,
    "bike": TravelType.bicycle,
    "bicicle": TravelType.bicycle,
    "cycling": TravelType.bicycle,
}

# OSMnx Overpass network_type for each travel mode
NETWORK_TYPE: dict[TravelType, str] = {
    TravelType.foot: "walk",
    TravelType.bicycle: "bike",
    TravelType.car: "drive",
}

# Highway values that are never useful as a public road
_EXCLUDED_ALL = {
    "abandoned",
    "construction",
    "planned",
    "platform",
    "proposed",
    "raceway",
    "no",
    "razed",
    "dismantled",
    "disused",
}

# Mode-specific highway exclusions, aligned with OSMnx Overpass filters
_EXCLUDED: dict[TravelType, set[str]] = {
    TravelType.car: _EXCLUDED_ALL
    | {
        "bridleway",
        "bus_guideway",
        "corridor",
        "cycleway",
        "elevator",
        "escalator",
        "footway",
        "path",
        "pedestrian",
        "steps",
        "track",
    },
    TravelType.foot: _EXCLUDED_ALL | {"motorway", "motorway_link"},
    TravelType.bicycle: _EXCLUDED_ALL
    | {"motorway", "motorway_link", "footway", "steps", "escalator", "elevator"},
}


def parse_travel_type(value: str | TravelType) -> TravelType:
    """Normalize user input (`feet`, `bicicle`, `bike`, ...) to a travel type."""
    if isinstance(value, TravelType):
        return value
    key = str(value).strip().lower()
    try:
        return _ALIASES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted({t.value for t in TravelType}))
        raise ValueError(f"Unknown travel type {value!r}. Expected one of: {allowed}.") from exc


def speed_kph(travel_type: str | TravelType, settings: Settings | None = None) -> float:
    """Average speed in km/h for the travel type."""
    from oisochron.config import get_settings

    settings = settings or get_settings()
    mode = parse_travel_type(travel_type)
    return {
        TravelType.foot: settings.speed_foot_kph,
        TravelType.bicycle: settings.speed_bicycle_kph,
        TravelType.car: settings.speed_car_kph,
    }[mode]


def max_distance_meters(
    travel_type: str | TravelType,
    travel_time_min: float,
    settings: Settings | None = None,
) -> float:
    """Straight-line download radius: speed * time * safety margin."""
    from oisochron.config import get_settings

    settings = settings or get_settings()
    meters_per_second = speed_kph(travel_type, settings) / 3.6
    return meters_per_second * travel_time_min * 60.0 * settings.download_margin


def highway_allowed(highway: object, travel_type: str | TravelType) -> bool:
    """Return True if an OSM highway tag is usable for the travel type."""
    mode = parse_travel_type(travel_type)
    excluded = _EXCLUDED[mode]
    values = highway if isinstance(highway, (list, tuple, set)) else [highway]
    tags = [str(v).strip().lower() for v in values if v is not None]
    if not tags:
        return False
    return all(tag not in excluded for tag in tags)
