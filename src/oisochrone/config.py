"""Load shared settings from the environment and the project `.env` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# src/oisochrone/config.py -> repository root
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


def parse_graph_preload(value: str | None, default: bool = True) -> bool:
    """Parse GRAPH_PRELOAD: true/tiles enable; false/off disable; full→tiles."""
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", "none"}:
        return False
    if normalized in {"1", "true", "yes", "on", "tiles", "tile"}:
        return True
    if normalized == "full":
        # Full-country RAM graphs are not implemented; tile preload is the path.
        return True
    return default


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


def _parse_csv_modes(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return default
    modes = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return modes or default


def _parse_csv_floats(raw: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    if raw is None or raw.strip() == "":
        return default
    values: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        values.append(float(part))
    return tuple(values) if values else default


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
    graph_cache_enabled: bool
    graph_cache_dir: Path
    graph_cache_memory_size: int
    graph_cache_grid_deg: float
    graph_sqlite_path: Path
    graph_preload: bool
    graph_seed_on_startup: bool
    graph_seed_coordinates_path: Path
    graph_seed_modes: tuple[str, ...]
    graph_seed_travel_times_min: tuple[float, ...]
    graph_seed_car_max_travel_time_min: float

    @property
    def use_pbf(self) -> bool:
        return self.osm_source == "pbf" and self.osm_pbf_path.is_file()

    @property
    def graph_seed_travel_time_min(self) -> float:
        """Largest configured seed time (compat for older callers)."""
        return max(self.graph_seed_travel_times_min) if self.graph_seed_travel_times_min else 15.0


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

    graph_cache_raw = os.getenv("GRAPH_CACHE_DIR")
    graph_cache_dir = (
        Path(graph_cache_raw).expanduser()
        if graph_cache_raw
        else data_dir / "graph_cache"
    )
    if not graph_cache_dir.is_absolute():
        graph_cache_dir = (_REPO_ROOT / graph_cache_dir).resolve()

    sqlite_raw = os.getenv("GRAPH_SQLITE_PATH")
    graph_sqlite_path = (
        Path(sqlite_raw).expanduser()
        if sqlite_raw
        else graph_cache_dir / "graph_tiles.sqlite"
    )
    if not graph_sqlite_path.is_absolute():
        graph_sqlite_path = (_REPO_ROOT / graph_sqlite_path).resolve()

    seed_raw = os.getenv("GRAPH_SEED_COORDINATES_PATH")
    if seed_raw:
        graph_seed_coordinates_path = Path(seed_raw).expanduser()
        if not graph_seed_coordinates_path.is_absolute():
            graph_seed_coordinates_path = (_REPO_ROOT / graph_seed_coordinates_path).resolve()
    else:
        graph_seed_coordinates_path = Path(__file__).resolve().parent / "data" / "coordinate_seed.json"

    times_raw = os.getenv("GRAPH_SEED_TRAVEL_TIMES_MIN")
    if times_raw is None or times_raw.strip() == "":
        # Backward compatible single-time env.
        single = os.getenv("GRAPH_SEED_TRAVEL_TIME_MIN")
        graph_seed_travel_times_min = (
            (float(single),) if single and single.strip() else (15.0, 30.0)
        )
    else:
        graph_seed_travel_times_min = _parse_csv_floats(times_raw, (15.0, 30.0))

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
        graph_cache_enabled=_as_bool(os.getenv("GRAPH_CACHE_ENABLED"), True),
        graph_cache_dir=graph_cache_dir,
        graph_cache_memory_size=int(_as_float("GRAPH_CACHE_MEMORY_SIZE", 8)),
        graph_cache_grid_deg=_as_float("GRAPH_CACHE_GRID_DEG", 0.05),
        graph_sqlite_path=graph_sqlite_path,
        graph_preload=parse_graph_preload(os.getenv("GRAPH_PRELOAD"), True),
        graph_seed_on_startup=_as_bool(os.getenv("GRAPH_SEED_ON_STARTUP"), True),
        graph_seed_coordinates_path=graph_seed_coordinates_path,
        graph_seed_modes=_parse_csv_modes(
            os.getenv("GRAPH_SEED_MODES"),
            ("foot", "bicycle", "car"),
        ),
        graph_seed_travel_times_min=graph_seed_travel_times_min,
        graph_seed_car_max_travel_time_min=_as_float("GRAPH_SEED_CAR_MAX_TRAVEL_TIME_MIN", 15.0),
    )


def clear_settings_cache() -> None:
    """Drop cached settings (used by tests)."""
    get_settings.cache_clear()
    from oisochrone.graph_cache import reset_graph_cache
    from oisochrone.seed import reset_seed_thread_state

    reset_graph_cache()
    reset_seed_thread_state()
