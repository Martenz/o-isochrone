# Performance: speeding up isochrone responses

This document describes two ways to cut API latency for o-isochrone:

1. **Proposed (implemented)** — SQLite graph tiles + warm in-memory preload in the current OSMnx / NetworkX stack
2. **Max speed** — dedicated routing engine (Valhalla / OSRM / GraphHopper) with prebuilt contraction hierarchies

## Why requests are slow today (without cache)

With `OSM_SOURCE=pbf`, each `/isochrone` call historically did:

1. `osmium extract` on the full highway PBF for a bbox
2. `osmium cat` to XML
3. OSMnx `graph_from_xml` + simplify + mode filter
4. Constant speeds → NetworkX ego-graph / Dijkstra
5. Shapely / H3 → GeoJSON

Steps 1–3 dominate. The isochrone walk itself is usually cheap once a graph exists. DuckDB or SQLite with spatial extensions do **not** replace graph search; they help analytics and `ST_*` queries, not weighted shortest paths. Here SQLite stores **prebuilt NetworkX tile blobs**, and routing still runs in Python.

```
Request ──► osmium clip PBF ──► OSMnx build ──► NetworkX ──► GeoJSON
                 ▲ bottleneck ▲
```

---

## Solution 1 — SQLite tiles + warm preload (proposed, implemented)

**Goal:** keep the Python library and API as they are, but avoid rebuilding the same road clip over and over — with no extra containers.

### Idea

- Expand each request bbox onto a fixed degree grid (e.g. `0.05°`) → one **tile** (quantized clip).
- Persist the **mode-filtered** OSMnx `MultiDiGraph` as a gzip-pickle BLOB in **SQLite** (`GRAPH_SQLITE_PATH`).
- **Warm / memory:** process-local LRU of the same keys.
- **`GRAPH_PRELOAD=tiles` (default):** at API startup, load up to `GRAPH_CACHE_MEMORY_SIZE` existing tiles for the current OSM fingerprint into RAM.
- Invalidate via PBF size/mtime fingerprint embedded in the cache key.

```
Startup ──► SQLite ──► warm LRU (GRAPH_PRELOAD=tiles)

Request ──► quantized bbox key
              ├─ memory LRU hit ──► NetworkX ──► GeoJSON
              ├─ SQLite hit ──────► warm LRU ──► NetworkX ──► GeoJSON
              └─ miss ──► osmium/OSMnx build ──► write SQLite + LRU ──► …
```

Nearby clicks with a similar travel radius share one tile. After the first miss (and after restart with preload), repeats are fast.

### Configuration

| Variable | Default | Role |
| --- | --- | --- |
| `GRAPH_CACHE_ENABLED` | `true` | Master switch |
| `GRAPH_CACHE_DIR` | `$DATA_DIR/graph_cache` | Directory for the SQLite file |
| `GRAPH_SQLITE_PATH` | `$GRAPH_CACHE_DIR/graph_tiles.sqlite` | Tile database |
| `GRAPH_CACHE_MEMORY_SIZE` | `8` | Max warm graphs in RAM (also preload cap) |
| `GRAPH_CACHE_GRID_DEG` | `0.05` | Bbox quantization step (degrees) |
| `GRAPH_PRELOAD` | `tiles` | `tiles`/`true` = warm SQLite→RAM at startup; `false` = on-demand only |
| `GRAPH_SEED_ON_STARTUP` | `true` | Background-seed missing tiles from `coordinate_seed.json` |
| `GRAPH_SEED_COORDINATES_PATH` | bundled file | Override seed points (match your OSM extract) |
| `GRAPH_SEED_MODES` | `foot,bicycle,car` | Modes to seed |
| `GRAPH_SEED_TRAVEL_TIMES_MIN` | `15,30` | Travel-time budgets to seed |
| `GRAPH_SEED_CAR_MAX_TRAVEL_TIME_MIN` | `15` | Cap car seed times (skip car@30 by default) |

Why SQLite (not DuckDB): one-file persistence, stdlib driver, fine for random tile reads. DuckDB remains optional later for analytics over many stored isochrones — not required for the hot path.

The bundled [`coordinate_seed.json`](../src/oisochrone/data/coordinate_seed.json) lists Italian province capitals for the **Italy demo PBF**. Replace those points when you use another extract.

A larger seeded tile (e.g. foot@30) is reused for shorter requests at the same snapped location (foot@15). Watch Compose logs for `[o-isochrone-seed]`.

### Expected gains

| Scenario | Typical effect |
| --- | --- |
| First request in an area | Same cost as before (build + write SQLite) |
| Repeat / nearby request (warm) | Tens of ms → low hundreds of ms for the graph step |
| Process restart + preload | Skip osmium/XML for tiles already in SQLite (up to memory cap) |

### Limits

- Does **not** replace Dijkstra with contraction hierarchies; large car radii still walk a big graph.
- Full Italy-wide “one graph in RAM at startup” is **not** implemented (`GRAPH_PRELOAD=full` falls back to tiles).
- **Bulk cold starts** still cost roughly *N × (osmium clip + OSMnx build)* when origins fall in different tiles. The demo nginx proxy times out around **300s**; the FE surfaces 504s clearly.
- Cache helps **repeat / nearby** requests. It does not make a first multi-city car bulk “instant.”

### What is already clipped (not whole Italy)

Each origin already uses only a **local portion** of the PBF:

1. `dist_m = speed_kph × travel_time_min × 60 × DOWNLOAD_MARGIN` (straight-line radius; margin default `1.2`)
2. Bbox around the origin for that distance
3. Expansion onto `GRAPH_CACHE_GRID_DEG` (default `0.05°`) so nearby requests share one tile
4. `osmium extract` of that bbox → OSMnx graph → ego-graph isochrone (on cache miss only)

Examples at default speeds: foot 15 min ≈ **1.5 km**, bike 15 min ≈ **4.5 km**, car 15 min ≈ **12 km**, car 30 min ≈ **24 km**.

### When to use

Default path for this repo: demo API, notebooks, and library users who want faster responses without operating another service.

---

## Solution 2 — Dedicated routing engine (max speed)

**Goal:** minimize time-to-first-byte for interactive map clicks at country scale.

### Idea

Offline, once:

1. Feed the same highway PBF into **Valhalla** (or OSRM / GraphHopper).
2. Build tiles / contraction hierarchies for walk / bike / auto profiles.

At request time:

1. FastAPI (or the FE via a thin proxy) calls the engine’s **isochrone** HTTP API.
2. Translate the polygon (and optional corridor) to the GeoJSON shape the demo already expects.

```
PBF ──► Valhalla build (once) ──► tiles on disk
Request ──► Valhalla /isochrone ──► wrap GeoJSON ──► FE
                 ▲ ms–low 100s ms ▲
```

### Why this is fastest

Routing engines precompute hierarchy / shortcuts. Isochrone queries become localized expansion on an optimized structure, not “clip PBF + build NetworkX + ego_graph” per click.

### Trade-offs

| | SQLite tile cache | Valhalla (etc.) |
| --- | --- | --- |
| Ops | None beyond this API | Extra container, disk for tiles, rebuild on OSM updates |
| Code | Stays in OSMnx/NetworkX | Adapter + profile mapping for speeds |
| Latency | Large win on cache hits | Best cold and warm at scale |
| Features | Exact current layer pipeline | May reimplement buffers / H3 in Python after the polygon |

### Suggested layout (future)

```yaml
# sketch only — not shipped yet
services:
  valhalla:
    image: …  # or custom build from italy-roads.osm.pbf
    volumes: ["./data/valhalla:/data"]
  api:
    environment:
      ISOCHRONE_BACKEND: valhalla
      VALHALLA_URL: http://valhalla:8002
```

### When to use

Choose this when Italy-wide (or multi-region) **car** isochrones must feel instant on every cold click, or when you outgrow NetworkX memory/CPU for large batches.

---

## Recommendation

1. **Ship / operate Solution 1 now** (`GRAPH_CACHE_ENABLED=true`, `GRAPH_PRELOAD=tiles`).
2. Measure p50/p95 on `/isochrone` for foot 15 min vs car 30 min after a warm-up pass over your usual cities.
3. If cold car queries or large batches are still too slow, add **Solution 2** as an optional backend and keep Solution 1 as the library default for offline notebooks.
