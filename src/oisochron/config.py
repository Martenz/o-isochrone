"""Load shared settings from the environment and the project `.env` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# src/oisochron/config.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    for candidate in (_REPO_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("OSM_BBOX must be west,south,east,north")
    west, south, east, north = (float(p) for p in parts)
    return west, south, east, north


def _resolve(path_value: str | None, default: Path, data_dir: Path) -> Path:
    if path_value:
        path = Path(path_value).expanduser()
    else:
        path = default
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
        # Prefer DATA_DIR for the conventional filenames when the relative
        # default would otherwise depend on the process working directory.
        if path.name == default.name and data_dir.joinpath(path.name) != path:
            nested = data_dir / path.name
            if nested.exists() or not path.exists():
                return nested
    return path


@dataclass(frozen=True)
class Settings:
    """Runtime configuration shared by the library and the example services."""

    data_dir: Path
    osm_source: str
    osm_pbf_url: str
    osm_pbf_path: Path
    osm_extent_path: Path
    osm_bbox: tuple[float, float, float, float]
    overpass_url: str
    cache_folder: Path
    use_cache: bool
    overpass_timeout: int
    speed_foot_kph: float
    speed_bicycle_kph: float
    speed_car_kph: float
    download_margin: float
    max_travel_time_min: float

    @property
    def use_pbf(self) -> bool:
        return self.osm_source == "pbf" and self.osm_pbf_path.is_file()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()

    data_dir = Path(os.getenv("DATA_DIR", str(_REPO_ROOT / "data"))).expanduser()
    if not data_dir.is_absolute():
        data_dir = (_REPO_ROOT / data_dir).resolve() if not data_dir.exists() else data_dir.resolve()

    cache_raw = os.getenv("OSMNX_CACHE_FOLDER")
    cache_folder = (
        Path(cache_raw).expanduser()
        if cache_raw
        else data_dir / "cache"
    )
    if not cache_folder.is_absolute():
        cache_folder = (_REPO_ROOT / cache_folder).resolve()

    return Settings(
        data_dir=data_dir,
        osm_source=os.getenv("OSM_SOURCE", "pbf").strip().lower(),
        osm_pbf_url=os.getenv(
            "OSM_PBF_URL",
            "https://download.geofabrik.de/europe/italy-latest.osm.pbf",
        ),
        osm_pbf_path=_resolve(
            os.getenv("OSM_PBF_PATH"),
            data_dir / "italy-roads.osm.pbf",
            data_dir,
        ),
        osm_extent_path=_resolve(
            os.getenv("OSM_EXTENT_PATH"),
            data_dir / "extent.geojson",
            data_dir,
        ),
        osm_bbox=_parse_bbox(os.getenv("OSM_BBOX", "6.627,35.493,18.521,47.092")),
        overpass_url=os.getenv("OVERPASS_URL", "https://overpass-api.de/api").rstrip("/"),
        cache_folder=cache_folder,
        use_cache=_as_bool(os.getenv("OSMNX_USE_CACHE"), True),
        overpass_timeout=int(_as_float("OSMNX_TIMEOUT", 180)),
        speed_foot_kph=_as_float("SPEED_FOOT_KPH", 5.0),
        speed_bicycle_kph=_as_float("SPEED_BICYCLE_KPH", 15.0),
        speed_car_kph=_as_float("SPEED_CAR_KPH", 40.0),
        download_margin=_as_float("DOWNLOAD_MARGIN", 1.2),
        max_travel_time_min=_as_float("MAX_TRAVEL_TIME_MIN", 120.0),
    )


def clear_settings_cache() -> None:
    """Drop cached settings (used by tests)."""
    get_settings.cache_clear()
