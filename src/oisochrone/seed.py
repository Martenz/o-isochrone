"""Seed SQLite graph tiles from a coordinate list (demo: Italian province capitals)."""

from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from oisochrone.config import Settings, get_settings
from oisochrone.exceptions import IsochroneError
from oisochrone.graph_cache import get_graph_cache, snap_clip, source_fingerprint
from oisochrone.network import configure_osmnx, load_road_network
from oisochrone.speeds import TravelType, max_distance_meters, parse_travel_type, speed_kph

logger = logging.getLogger(__name__)

_PACKAGE_SEED = Path(__file__).resolve().parent / "data" / "coordinate_seed.json"
_SEED_THREAD: threading.Thread | None = None
_SEED_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SeedPoint:
    lat: float
    lon: float
    id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SeedResult:
    total: int
    built: int
    skipped: int
    failed: int


def _seed_log(message: str, *args: object) -> None:
    text = message % args if args else message
    logger.info(text)
    print(f"[o-isochrone-seed] {text}", file=sys.stderr, flush=True)


def default_coordinate_seed_path() -> Path:
    """Bundled seed file shipped with the library (Italy demo capitals)."""
    return _PACKAGE_SEED


def load_coordinate_seed(path: Path | None = None) -> list[SeedPoint]:
    """Load seed points from JSON.

    Accepts either ``{"points": [...]}`` or a bare list of point objects.
    Each point needs ``lat`` / ``lon`` (aliases ``latitude`` / ``longitude`` / ``lng``).
    """
    seed_path = path or default_coordinate_seed_path()
    if not seed_path.is_file():
        raise FileNotFoundError(f"Coordinate seed file not found: {seed_path}")
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("points")
        if not isinstance(rows, list):
            raise ValueError(f"{seed_path} must contain a 'points' array")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"{seed_path} must be a JSON object or array")

    points: list[SeedPoint] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if "lat" not in row and "latitude" not in row:
            continue
        lat = row.get("lat", row.get("latitude"))
        lon = row.get("lon", row.get("lng", row.get("longitude")))
        if lat is None or lon is None:
            raise ValueError(f"seed point {index} is missing lat/lon")
        ident = row.get("id")
        name = row.get("name")
        points.append(
            SeedPoint(
                lat=float(lat),
                lon=float(lon),
                id=None if ident is None else str(ident),
                name=None if name is None else str(name),
            )
        )
    if not points:
        raise ValueError(f"No seed points with lat/lon in {seed_path}")
    return points


def expand_seed_jobs(
    modes: list[str] | tuple[str, ...],
    times_min: list[float] | tuple[float, ...],
    *,
    car_max_travel_time_min: float,
) -> list[tuple[TravelType, float]]:
    """Expand mode × time pairs, capping car at *car_max_travel_time_min*."""
    jobs: list[tuple[TravelType, float]] = []
    for raw_mode in modes:
        mode = parse_travel_type(raw_mode)
        for minutes in times_min:
            minutes_f = float(minutes)
            if minutes_f <= 0:
                raise ValueError("seed travel times must be positive")
            if mode is TravelType.car and minutes_f > car_max_travel_time_min + 1e-9:
                continue
            jobs.append((mode, minutes_f))
    if not jobs:
        raise ValueError("No seed jobs after applying mode/time filters")
    return jobs


def seed_graph_tiles(
    settings: Settings | None = None,
    *,
    coordinates_path: Path | None = None,
    modes: list[str] | tuple[str, ...] | None = None,
    travel_times_min: list[float] | tuple[float, ...] | None = None,
    travel_time_min: float | None = None,
    progress_every: int = 5,
) -> SeedResult:
    """Build missing graph tiles for each seed point × mode × time into SQLite.

    Existing tiles that already cover the needed radius are skipped. Safe to re-run.
    """
    settings = settings or get_settings()
    if not settings.graph_cache_enabled:
        _seed_log("Graph cache disabled; skipping tile seed")
        return SeedResult(total=0, built=0, skipped=0, failed=0)

    configure_osmnx(settings)
    cache = get_graph_cache(settings)
    fingerprint = source_fingerprint(settings)
    path = coordinates_path or settings.graph_seed_coordinates_path
    points = load_coordinate_seed(path)
    seed_modes = modes if modes is not None else settings.graph_seed_modes
    if travel_times_min is not None:
        times = tuple(float(t) for t in travel_times_min)
    elif travel_time_min is not None:
        times = (float(travel_time_min),)
    else:
        times = settings.graph_seed_travel_times_min

    mode_time_jobs = expand_seed_jobs(
        seed_modes,
        times,
        car_max_travel_time_min=settings.graph_seed_car_max_travel_time_min,
    )
    jobs = [(point, mode, minutes) for point in points for mode, minutes in mode_time_jobs]
    built = skipped = failed = 0
    _seed_log(
        "Seeding %s point(s) × %s mode/time combo(s) = %s job(s) from %s "
        "(modes=%s times=%s car_max=%s)",
        len(points),
        len(mode_time_jobs),
        len(jobs),
        path,
        ",".join(seed_modes),
        ",".join(str(t) for t in times),
        settings.graph_seed_car_max_travel_time_min,
    )

    for index, (point, mode, minutes) in enumerate(jobs, start=1):
        label = point.name or point.id or f"{point.lat},{point.lon}"
        try:
            dist_m = max_distance_meters(mode, minutes, settings)
            kmh = speed_kph(mode, settings)
            lat_s, lon_s, dist_s = snap_clip(point.lat, point.lon, dist_m, cache.grid_deg)
            if cache.covers(
                mode=mode.value,
                lat_s=lat_s,
                lon_s=lon_s,
                dist_s=dist_s,
                fingerprint=fingerprint,
                speed_kph=kmh,
            ):
                skipped += 1
            else:
                _seed_log(
                    "Building %s/%s %s %s %.0fmin (clip≈%.0fm)",
                    index,
                    len(jobs),
                    label,
                    mode.value,
                    minutes,
                    dist_s,
                )
                load_road_network(point.lat, point.lon, dist_m, mode, settings)
                built += 1
        except (IsochroneError, ValueError, OSError) as exc:
            failed += 1
            _seed_log("FAILED %s (%s %.0fmin): %s", label, mode.value, minutes, exc)
            logger.warning("Seed failed for %s (%s %.0fmin): %s", label, mode.value, minutes, exc)
        if progress_every > 0 and index % progress_every == 0:
            _seed_log(
                "Progress %s/%s (built=%s skipped=%s failed=%s)",
                index,
                len(jobs),
                built,
                skipped,
                failed,
            )

    result = SeedResult(total=len(jobs), built=built, skipped=skipped, failed=failed)
    _seed_log(
        "Finished: total=%s built=%s skipped=%s failed=%s",
        result.total,
        result.built,
        result.skipped,
        result.failed,
    )
    return result


def start_graph_seed_background(settings: Settings | None = None) -> bool:
    """Start a daemon thread that seeds tiles if ``GRAPH_SEED_ON_STARTUP`` is on.

    Returns True when a new thread was started.
    """
    global _SEED_THREAD
    settings = settings or get_settings()
    if not settings.graph_seed_on_startup or not settings.graph_cache_enabled:
        _seed_log(
            "Startup seed disabled (GRAPH_SEED_ON_STARTUP=%s GRAPH_CACHE_ENABLED=%s)",
            settings.graph_seed_on_startup,
            settings.graph_cache_enabled,
        )
        return False

    def _run() -> None:
        try:
            seed_graph_tiles(settings)
        except Exception:
            logger.exception("Background graph tile seed failed")
            print(
                "[o-isochrone-seed] Background graph tile seed failed; see logs",
                file=sys.stderr,
                flush=True,
            )

    with _SEED_LOCK:
        if _SEED_THREAD is not None and _SEED_THREAD.is_alive():
            _seed_log("Seed thread already running; not starting another")
            return False
        _SEED_THREAD = threading.Thread(
            target=_run,
            name="oisochrone-graph-seed",
            daemon=True,
        )
        _SEED_THREAD.start()
        _seed_log("Started background graph tile seed thread")
        return True


def reset_seed_thread_state() -> None:
    """Test helper: clear the background-seed thread handle."""
    global _SEED_THREAD
    with _SEED_LOCK:
        _SEED_THREAD = None
