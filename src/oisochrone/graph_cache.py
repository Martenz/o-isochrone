"""Warm in-memory + SQLite-backed cache for clipped OSMnx road graphs."""

from __future__ import annotations

import gzip
import logging
import math
import pickle
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oisochrone.config import Settings

logger = logging.getLogger(__name__)

# Bump when the blob layout or graph schema / cache-key scheme changes.
CACHE_FORMAT_VERSION = 3

BBox = tuple[float, float, float, float]  # west, south, east, north

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_tiles (
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
    accessed_at REAL NOT NULL,
    lat_s REAL,
    lon_s REAL,
    dist_s REAL,
    speed_kph REAL
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_graph_tiles_fingerprint
    ON graph_tiles (fingerprint, accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_graph_tiles_cover
    ON graph_tiles (fingerprint, mode, lat_s, lon_s, speed_kph, dist_s);
"""

_SNAP_COLUMNS = (
    ("lat_s", "REAL"),
    ("lon_s", "REAL"),
    ("dist_s", "REAL"),
    ("speed_kph", "REAL"),
)

def quantize_bbox(bbox: BBox, grid_deg: float) -> BBox:
    """Expand *bbox* outward onto a fixed degree grid (legacy helper / tests)."""
    if grid_deg <= 0:
        raise ValueError("grid_deg must be positive")
    west, south, east, north = bbox
    if east < west or north < south:
        raise ValueError("bbox must be west,south,east,north with positive span")
    q_west = math.floor(west / grid_deg) * grid_deg
    q_south = math.floor(south / grid_deg) * grid_deg
    q_east = math.ceil(east / grid_deg) * grid_deg
    q_north = math.ceil(north / grid_deg) * grid_deg
    if q_east <= q_west:
        q_east = q_west + grid_deg
    if q_north <= q_south:
        q_north = q_south + grid_deg
    return (q_west, q_south, q_east, q_north)


def snap_clip(
    lat: float,
    lon: float,
    dist_m: float,
    grid_deg: float,
) -> tuple[float, float, float]:
    """Snap origin finely and bucket the download radius.

    Uses a step finer than ``grid_deg`` so clips stay close to the true travel
    radius (important for car, where a coarse snap + large margin previously
    produced ~17 km extracts that could OOM the API container).
    """
    if grid_deg <= 0:
        raise ValueError("grid_deg must be positive")
    if dist_m <= 0:
        raise ValueError("dist_m must be positive")
    # ~0.01° (~1.1 km) by default when grid is 0.05°.
    step = min(max(grid_deg / 5.0, 0.005), 0.02)
    lat_s = round(float(lat) / step) * step
    lon_s = round(float(lon) / step) * step
    bucket = 500.0
    snap_m = step * 111_320.0 * math.sqrt(0.5)
    needed = float(dist_m) + snap_m
    dist_s = math.ceil(needed / bucket) * bucket
    return lat_s, lon_s, dist_s


def source_fingerprint(settings: Settings) -> str:
    """Token that invalidates disk entries when the OSM source changes.

    Uses the PBF basename (not absolute path) so the same file shared via a
    Docker volume keeps a stable key on host and in-container.
    """
    if settings.use_pbf:
        path = settings.osm_pbf_path
        try:
            stat = path.stat()
        except OSError:
            return f"pbf:missing:{path.name}"
        return f"pbf:{path.name}:{stat.st_mtime_ns}:{stat.st_size}"
    return f"overpass:{settings.overpass_url}"


def cache_key(
    mode: str,
    lat_s: float,
    lon_s: float,
    dist_s: float,
    fingerprint: str,
    *,
    speed_kph: float,
) -> str:
    return (
        f"v{CACHE_FORMAT_VERSION}|{mode}|"
        f"{lat_s:.5f}|{lon_s:.5f}|{dist_s:.0f}|{speed_kph:.2f}|{fingerprint}"
    )


def parse_cache_key(key: str) -> dict[str, Any] | None:
    """Parse a cache key into mode / snap / distance / speed / fingerprint."""
    parts = key.split("|")
    if len(parts) < 7:
        return None
    try:
        return {
            "version": parts[0],
            "mode": parts[1],
            "lat_s": float(parts[2]),
            "lon_s": float(parts[3]),
            "dist_s": float(parts[4]),
            "speed_kph": float(parts[5]),
            "fingerprint": "|".join(parts[6:]),
        }
    except ValueError:
        return None


@dataclass
class CacheStats:
    memory_hits: int = 0
    disk_hits: int = 0
    misses: int = 0
    stores: int = 0
    preloaded: int = 0
    cover_hits: int = 0


class GraphCache:
    """LRU memory cache backed by gzip-pickled graphs in SQLite."""

    def __init__(
        self,
        *,
        enabled: bool,
        cache_dir: Path,
        sqlite_path: Path,
        memory_size: int,
        grid_deg: float,
        preload: bool,
    ) -> None:
        self.enabled = enabled
        self.cache_dir = cache_dir
        self.sqlite_path = sqlite_path
        self.memory_size = max(0, int(memory_size))
        self.grid_deg = float(grid_deg)
        self.preload_enabled = preload
        self._memory: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.stats = CacheStats()
        if self.enabled:
            self._ensure_db()

    def _ensure_db(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.sqlite_path),
            check_same_thread=False,
            timeout=60.0,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Create the table first, migrate missing columns, then indexes that
        # reference those columns (CREATE INDEX on a pre-migration DB would
        # otherwise fail with "no such column: lat_s").
        conn.executescript(_TABLE_SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(graph_tiles)")}
        for name, decl in _SNAP_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE graph_tiles ADD COLUMN {name} {decl}")
        conn.executescript(_INDEX_SCHEMA)
        # Drop legacy oversize tiles from earlier cache-key schemes.
        conn.execute(
            "DELETE FROM graph_tiles WHERE format_version != ?",
            (CACHE_FORMAT_VERSION,),
        )
        conn.commit()
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def clear_memory(self) -> None:
        with self._lock:
            self._memory.clear()

    def clear(self) -> None:
        """Drop warm entries and delete all SQLite tiles."""
        with self._lock:
            self._memory.clear()
            if not self.enabled:
                return
            conn = self._ensure_db()
            conn.execute("DELETE FROM graph_tiles")
            conn.commit()

    def contains(self, key: str) -> bool:
        """Return True if *key* is in memory or SQLite."""
        if not self.enabled:
            return False
        with self._lock:
            if key in self._memory:
                return True
            conn = self._ensure_db()
            row = conn.execute(
                """
                SELECT 1 FROM graph_tiles
                WHERE cache_key = ? AND format_version = ?
                LIMIT 1
                """,
                (key, CACHE_FORMAT_VERSION),
            ).fetchone()
            return row is not None

    def preload(self, fingerprint: str | None = None) -> int:
        """Load existing SQLite tiles into the warm LRU (up to memory_size).

        When *fingerprint* is set, only tiles for that OSM source are loaded.
        Returns how many graphs were placed in memory.
        """
        if not self.enabled or not self.preload_enabled or self.memory_size <= 0:
            return 0
        with self._lock:
            conn = self._ensure_db()
            if fingerprint is None:
                rows = conn.execute(
                    """
                    SELECT cache_key, graph_blob FROM graph_tiles
                    WHERE format_version = ?
                    ORDER BY accessed_at DESC
                    LIMIT ?
                    """,
                    (CACHE_FORMAT_VERSION, self.memory_size),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT cache_key, graph_blob FROM graph_tiles
                    WHERE format_version = ? AND fingerprint = ?
                    ORDER BY accessed_at DESC
                    LIMIT ?
                    """,
                    (CACHE_FORMAT_VERSION, fingerprint, self.memory_size),
                ).fetchall()

            loaded = 0
            for key, blob in rows:
                graph = self._decode_blob(blob)
                if graph is None:
                    continue
                self._put_memory(key, graph)
                loaded += 1
            self.stats.preloaded += loaded
            if loaded:
                logger.info(
                    "Preloaded %s graph tile(s) from SQLite into memory (cap=%s)",
                    loaded,
                    self.memory_size,
                )
            return loaded

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        with self._lock:
            if key in self._memory:
                self._memory.move_to_end(key)
                self.stats.memory_hits += 1
                logger.debug("graph cache memory hit: %s", key)
                return self._memory[key]

            graph = self._load_sqlite(key)
            if graph is None:
                self.stats.misses += 1
                return None

            self.stats.disk_hits += 1
            self._put_memory(key, graph)
            logger.debug("graph cache sqlite hit: %s", key)
            return graph

    def get_covering(
        self,
        *,
        mode: str,
        lat_s: float,
        lon_s: float,
        dist_s: float,
        fingerprint: str,
        speed_kph: float,
    ) -> Any | None:
        """Return an exact tile or the smallest cached tile with dist_s >= needed."""
        if not self.enabled:
            return None
        exact = cache_key(
            mode, lat_s, lon_s, dist_s, fingerprint, speed_kph=speed_kph
        )
        hit = self.get(exact)
        if hit is not None:
            return hit

        with self._lock:
            # Warm LRU may already hold a larger tile for this snap point.
            best_key: str | None = None
            best_dist = float("inf")
            for key in self._memory:
                parsed = parse_cache_key(key)
                if parsed is None:
                    continue
                if (
                    parsed["mode"] == mode
                    and parsed["fingerprint"] == fingerprint
                    and abs(parsed["lat_s"] - lat_s) < 1e-8
                    and abs(parsed["lon_s"] - lon_s) < 1e-8
                    and abs(parsed["speed_kph"] - speed_kph) < 0.011
                    and parsed["dist_s"] + 1e-6 >= dist_s
                    and parsed["dist_s"] < best_dist
                ):
                    best_key = key
                    best_dist = parsed["dist_s"]
            if best_key is not None:
                self._memory.move_to_end(best_key)
                self.stats.memory_hits += 1
                self.stats.cover_hits += 1
                self.stats.misses = max(0, self.stats.misses - 1)
                logger.info(
                    "graph cache cover hit (memory) mode=%s need=%.0fm have=%.0fm",
                    mode,
                    dist_s,
                    best_dist,
                )
                return self._memory[best_key]

            conn = self._ensure_db()
            row = conn.execute(
                """
                SELECT cache_key, graph_blob, dist_s FROM graph_tiles
                WHERE format_version = ?
                  AND mode = ?
                  AND fingerprint = ?
                  AND lat_s IS NOT NULL
                  AND abs(lat_s - ?) < 1e-8
                  AND abs(lon_s - ?) < 1e-8
                  AND abs(speed_kph - ?) < 0.011
                  AND dist_s >= ?
                ORDER BY dist_s ASC
                LIMIT 1
                """,
                (
                    CACHE_FORMAT_VERSION,
                    mode,
                    fingerprint,
                    lat_s,
                    lon_s,
                    speed_kph,
                    dist_s,
                ),
            ).fetchone()
            if row is None:
                return None
            key, blob, have_dist = row
            graph = self._decode_blob(blob)
            if graph is None:
                return None
            now = time.time()
            conn.execute(
                "UPDATE graph_tiles SET accessed_at = ? WHERE cache_key = ?",
                (now, key),
            )
            conn.commit()
            self._put_memory(key, graph)
            self.stats.disk_hits += 1
            self.stats.cover_hits += 1
            self.stats.misses = max(0, self.stats.misses - 1)
            logger.info(
                "graph cache cover hit (sqlite) mode=%s need=%.0fm have=%.0fm",
                mode,
                dist_s,
                float(have_dist),
            )
            return graph

    def covers(
        self,
        *,
        mode: str,
        lat_s: float,
        lon_s: float,
        dist_s: float,
        fingerprint: str,
        speed_kph: float,
    ) -> bool:
        """True if an exact or larger tile already exists for this snap point."""
        if not self.enabled:
            return False
        exact = cache_key(
            mode, lat_s, lon_s, dist_s, fingerprint, speed_kph=speed_kph
        )
        if self.contains(exact):
            return True
        with self._lock:
            for key in self._memory:
                parsed = parse_cache_key(key)
                if parsed is None:
                    continue
                if (
                    parsed["mode"] == mode
                    and parsed["fingerprint"] == fingerprint
                    and abs(parsed["lat_s"] - lat_s) < 1e-8
                    and abs(parsed["lon_s"] - lon_s) < 1e-8
                    and abs(parsed["speed_kph"] - speed_kph) < 0.011
                    and parsed["dist_s"] + 1e-6 >= dist_s
                ):
                    return True
            conn = self._ensure_db()
            row = conn.execute(
                """
                SELECT 1 FROM graph_tiles
                WHERE format_version = ?
                  AND mode = ?
                  AND fingerprint = ?
                  AND lat_s IS NOT NULL
                  AND abs(lat_s - ?) < 1e-8
                  AND abs(lon_s - ?) < 1e-8
                  AND abs(speed_kph - ?) < 0.011
                  AND dist_s >= ?
                LIMIT 1
                """,
                (
                    CACHE_FORMAT_VERSION,
                    mode,
                    fingerprint,
                    lat_s,
                    lon_s,
                    speed_kph,
                    dist_s,
                ),
            ).fetchone()
            return row is not None

    def put(self, key: str, graph: Any, *, meta: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._put_memory(key, graph)
            self._store_sqlite(key, graph, meta=meta or {})
            self.stats.stores += 1

    def _put_memory(self, key: str, graph: Any) -> None:
        if self.memory_size <= 0:
            return
        self._memory[key] = graph
        self._memory.move_to_end(key)
        while len(self._memory) > self.memory_size:
            self._memory.popitem(last=False)

    def _encode_blob(self, key: str, graph: Any) -> bytes:
        payload = {"version": CACHE_FORMAT_VERSION, "key": key, "graph": graph}
        return gzip.compress(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))

    def _decode_blob(self, blob: bytes) -> Any | None:
        try:
            payload = pickle.loads(gzip.decompress(blob))
        except Exception:
            logger.exception("failed to decode graph tile blob")
            return None
        if not isinstance(payload, dict) or payload.get("version") != CACHE_FORMAT_VERSION:
            logger.warning("graph tile format mismatch; ignoring blob")
            return None
        return payload.get("graph")

    def _load_sqlite(self, key: str) -> Any | None:
        conn = self._ensure_db()
        row = conn.execute(
            """
            SELECT graph_blob FROM graph_tiles
            WHERE cache_key = ? AND format_version = ?
            """,
            (key, CACHE_FORMAT_VERSION),
        ).fetchone()
        if row is None:
            return None
        graph = self._decode_blob(row[0])
        if graph is None:
            return None
        now = time.time()
        conn.execute(
            "UPDATE graph_tiles SET accessed_at = ? WHERE cache_key = ?",
            (now, key),
        )
        conn.commit()
        return graph

    def _store_sqlite(self, key: str, graph: Any, *, meta: dict[str, Any]) -> None:
        conn = self._ensure_db()
        now = time.time()
        mode = str(meta.get("mode", ""))
        west, south, east, north = meta.get("bbox", (0.0, 0.0, 0.0, 0.0))
        fingerprint = str(meta.get("fingerprint", ""))
        lat_s = meta.get("lat_s")
        lon_s = meta.get("lon_s")
        dist_s = meta.get("dist_s")
        speed = meta.get("speed_kph")
        parsed = parse_cache_key(key)
        if parsed is not None:
            lat_s = parsed["lat_s"] if lat_s is None else lat_s
            lon_s = parsed["lon_s"] if lon_s is None else lon_s
            dist_s = parsed["dist_s"] if dist_s is None else dist_s
            speed = parsed["speed_kph"] if speed is None else speed
        blob = self._encode_blob(key, graph)
        try:
            conn.execute(
                """
                INSERT INTO graph_tiles (
                    cache_key, mode, west, south, east, north,
                    fingerprint, format_version, graph_blob, created_at, accessed_at,
                    lat_s, lon_s, dist_s, speed_kph
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    graph_blob = excluded.graph_blob,
                    format_version = excluded.format_version,
                    fingerprint = excluded.fingerprint,
                    accessed_at = excluded.accessed_at,
                    lat_s = excluded.lat_s,
                    lon_s = excluded.lon_s,
                    dist_s = excluded.dist_s,
                    speed_kph = excluded.speed_kph
                """,
                (
                    key,
                    mode,
                    float(west),
                    float(south),
                    float(east),
                    float(north),
                    fingerprint,
                    CACHE_FORMAT_VERSION,
                    blob,
                    now,
                    now,
                    None if lat_s is None else float(lat_s),
                    None if lon_s is None else float(lon_s),
                    None if dist_s is None else float(dist_s),
                    None if speed is None else float(speed),
                ),
            )
            conn.commit()
        except Exception:
            logger.exception("failed to write graph tile to SQLite")


_CACHE: GraphCache | None = None
_CACHE_LOCK = threading.Lock()
_PRELOADED = False


def get_graph_cache(settings: Settings | None = None) -> GraphCache:
    """Return the process-wide graph cache (created from settings once)."""
    global _CACHE
    from oisochrone.config import get_settings

    settings = settings or get_settings()
    with _CACHE_LOCK:
        if _CACHE is None:
            sqlite_path = settings.graph_sqlite_path
            _CACHE = GraphCache(
                enabled=settings.graph_cache_enabled,
                cache_dir=settings.graph_cache_dir,
                sqlite_path=sqlite_path,
                memory_size=settings.graph_cache_memory_size,
                grid_deg=settings.graph_cache_grid_deg,
                preload=settings.graph_preload,
            )
        return _CACHE


def preload_graph_cache(settings: Settings | None = None) -> int:
    """Warm memory from SQLite once per process when GRAPH_PRELOAD is enabled."""
    global _PRELOADED
    from oisochrone.config import get_settings

    settings = settings or get_settings()
    cache = get_graph_cache(settings)
    with _CACHE_LOCK:
        if _PRELOADED or not settings.graph_preload:
            return 0
        count = cache.preload(source_fingerprint(settings))
        _PRELOADED = True
        return count


def reset_graph_cache() -> None:
    """Drop the process-wide cache instance (tests / settings reload)."""
    global _CACHE, _PRELOADED
    with _CACHE_LOCK:
        if _CACHE is not None:
            _CACHE.clear_memory()
            _CACHE.close()
        _CACHE = None
        _PRELOADED = False
