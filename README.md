# o-isochrone

Python library that downloads a **portion** of the OpenStreetMap **road** network with [OSMnx](https://osmnx.readthedocs.io/), then computes a travel-time isochrone.

Given a latitude/longitude, a travel time, and a travel type (`foot`, `car`, `bicycle`), it returns GeoJSON layers you can mix:

- **buffered_lines** (default) — metric corridor around the reachable roads
- **lines** — reachable road polylines
- **filled_buffer** — the same corridor with enclosed interiors filled (the usual “isochrone polygon”)
- **convex_hull** — outer envelope of the reachable network (overestimates gaps)
- **concave_hull** — tighter envelope; can cut through long road segments
- **h3** — Uber H3 hexagons covering the reachable corridor

Average speeds come from the shared `.env` file (`SPEED_FOOT_KPH`, `SPEED_BICYCLE_KPH`, `SPEED_CAR_KPH`). Aliases such as `feet` and `bicicle` are accepted.

The FastAPI app and MapLibre demo are **examples** of using the library, not the core product. The demo can compute a single origin (click the map) or a **bulk** set of origins (Bulk button in the title card).

## Project layout

```
src/oisochrone/     # library (the main product)
api/               # example FastAPI service
demo/              # example MapLibre + OpenFreeMap page (nginx)
docs/              # design notes (performance / caching)
notebook_examples/ # Jupyter examples of the library (single + batch)
scripts/           # Italy PBF download / highway filter
tests/
```

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose to run the example services
- Optional: [osmium-tool](https://osmcode.org/osmium-tool/) for a faster PBF filter (`brew install osmium-tool`). The download script falls back to pyosmium.

## Setup with uv

```bash
cp .env.example .env
uv sync --extra api
```

This creates `.venv`, installs the library in editable mode, and writes `uv.lock`.

Useful commands:

```bash
uv run pytest
uv run uvicorn api.main:app --reload --port 8000
uv run python scripts/download_italy.py
uv add some-package          # add a library dependency
uv add --optional api fastapi
uv add --group dev pytest
uv sync --extra notebooks    # Jupyter examples
```

There is no Poetry project file. Dependency resolution is entirely uv.

## Library usage

```python
from oisochrone import (
    IsochroneOrigin,
    compute_isochrone,
    compute_isochrones,
    compute_isochrones_async,
    iter_isochrones,
    aiter_isochrones,
    get_network_extent,
)

collection = compute_isochrone(
    lat=45.4642,
    lon=9.1900,
    travel_time_min=15,
    travel_type="foot",   # foot | car | bicycle
    include="buffered_lines",  # or "lines,filled_buffer,h3"
    buffer_meters=10,
    h3_resolution=10,
    origin_id="duomo",  # optional; stamped on metadata + features
)

collections = compute_isochrones(
    [
        IsochroneOrigin(45.4642, 9.1900, id="duomo"),
        IsochroneOrigin(45.4863, 9.2045),  # id becomes "1"
        IsochroneOrigin(45.4515, 9.1750, id="navigli"),
    ],
    travel_time_min=12,
    travel_type="foot",
    include="buffered_lines",
    max_concurrency=3,
)
# collections is a list of FeatureCollections; filter with properties.origin_id

# Progressive batch: yield each origin as it finishes (completion order).
for result in iter_isochrones(
    [IsochroneOrigin(45.4642, 9.1900, id="duomo"), IsochroneOrigin(45.4863, 9.2045)],
    travel_time_min=12,
    travel_type="foot",
    max_concurrency=2,
):
    if result.ok:
        print(result.origin_id, len(result.collection["features"]))
    else:
        print(result.origin_id, "failed:", result.error)

extent = get_network_extent()  # GeoJSON polygon of the configured extract
```

`include` is a comma-separated (or list) set of layers. Default is `buffered_lines`. Legacy values `both` and `polygon` still work (`both` → `lines,filled_buffer`).

Batch helpers assign incremental string ids (`"1"`, `"2"`, …) when an origin omits `id`, skipping any ids already provided. The async variant `compute_isochrones_async` parallelizes with `asyncio.to_thread` (use it from FastAPI or `await` in Jupyter). For progressive UIs, prefer `iter_isochrones` / `aiter_isochrones`: they yield `BatchOriginResult` values in completion order and isolate per-origin failures instead of aborting the batch.

A walkthrough lives in [`notebook_examples/isochrone_usage.ipynb`](notebook_examples/isochrone_usage.ipynb).

## OSM road network source

The library always loads **only the portion** of the network needed for the request (travel time × average speed × `DOWNLOAD_MARGIN`). Configuration lives in the root `.env` and is shared by the library, API, and demo.

| Variable | Role |
| --- | --- |
| `OSM_SOURCE` | `pbf` (local extract) or `overpass` |
| `OSM_PBF_PATH` | Highway-only PBF used when `OSM_SOURCE=pbf` |
| `OSM_PBF_URL` | Source used by `scripts/download_italy.py` |
| `OSM_EXTENT_PATH` | GeoJSON extent shown by the demo |
| `OSM_BBOX` | Fallback west,south,east,north |
| `OVERPASS_URL` | OSMnx Overpass endpoint |
| `SPEED_*_KPH` | Average speeds per travel type |
| `GRAPH_CACHE_*` | Warm LRU + SQLite tile store for clipped graphs |
| `GRAPH_SQLITE_PATH` | SQLite file for persisted graph tiles |
| `GRAPH_PRELOAD` | `tiles` / `true` (default): warm SQLite tiles into RAM at API startup |
| `GRAPH_SEED_*` | Prefill SQLite from `coordinate_seed.json` (see below) |

If `OSM_SOURCE=pbf` but the file is missing, the library falls back to Overpass so the demo still works.

Clipped road graphs are stored in **SQLite** under `data/graph_cache/` (gitignored, Compose-mounted) and warmed into a process-local LRU (`GRAPH_PRELOAD=tiles` by default). See [docs/performance.md](docs/performance.md).

### Seeding the graph tile cache

Cold osmium/OSMnx builds are slow. Prefill SQLite for the places you care about so the demo (and bulk jobs near those points) hit warm tiles.

1. **Seed file** — [`src/oisochrone/data/coordinate_seed.json`](src/oisochrone/data/coordinate_seed.json)  
   - Generic list of `{id, name, lat, lon}` points under a `points` array.  
   - **Shipped prefilled with 107 Italian province / metropolitan capitals** for the Italy demo extract only.  
   - For another country or region, replace `points` with centres that cover **your** downloaded PBF (cities, hubs, grid samples). Point `GRAPH_SEED_COORDINATES_PATH` at your file if you keep it outside the package.

2. **Automatic (API startup)** — with `GRAPH_SEED_ON_STARTUP=true` (default), the API starts a **background** thread that builds missing tiles for each seed point × mode × time. Defaults:

   - `GRAPH_SEED_MODES=foot,bicycle,car`
   - `GRAPH_SEED_TRAVEL_TIMES_MIN=15,30`
   - `GRAPH_SEED_CAR_MAX_TRAVEL_TIME_MIN=15` (no car@30 — those clips are too large)

   Watch `api` logs for lines starting with `[o-isochrone-seed]`. Already-cached or **larger covering** tiles are skipped. A seeded **30 min** foot/bike tile also satisfies shorter times at the same snap point.

3. **Manual**

```bash
uv run python scripts/seed_graph_tiles.py
uv run python scripts/seed_graph_tiles.py --modes foot,bicycle --times 15,30
uv run python scripts/seed_graph_tiles.py --coordinates ./data/my_region_seed.json

docker compose run --rm api uv run --no-dev python scripts/seed_graph_tiles.py
```

Expect on the order of **hundreds of jobs** (107 cities × 5 mode/time combos). First full seed can take a long time; progress is logged every few jobs. Disk grows with each tile (especially bicycle@30 and car@15).

### Italy extract for demo and tests

Geofabrik’s Italy PBF is large (~2 GB). The download script keeps **all `highway=*` ways** (motorways through residential, plus links, secondary, tertiary, cycleways, footways, …) and drops everything else:

```bash
uv run python scripts/download_italy.py
# optional: uv run python scripts/download_italy.py --drop-full
```

Or, without any host tools, use the API image (it already includes osmium):

```bash
docker compose --profile download run --rm download
```

That writes `data/italy-roads.osm.pbf` and updates `data/extent.geojson`. OSM data is [ODbL](https://opendatacommons.org/licenses/odbl/).

## Example API

```bash
uv run uvicorn api.main:app --reload --port 8000
```

- `GET /health`
- `GET /config` (demo geocoder plugin from `.env`)
- `GET /network/extent`
- `GET|POST /isochrone` with `lat`, `lon`, `travel_time_min`, `travel_type`
- optional `origin_id` (stamped on the FeatureCollection and each feature)
- optional `include=buffered_lines` (comma-separated: `lines`, `buffered_lines`, `filled_buffer`, `convex_hull`, `concave_hull`, `h3`)
- optional `buffer_meters` (corridor width for buffer layers; default 10)
- optional `h3_resolution` (0–15, default 10 when `h3` is included)
- `POST /isochrone/batch` with `origins: [{lat, lon, id?}, …]` plus the same shared travel parameters; returns `{count, collections}` (one FeatureCollection per origin)
- `POST /isochrone/batch/stream` — same body; responds with NDJSON events (`start` / `result` / `error` / `done`) as each origin finishes so clients can paint incrementally

OpenAPI: http://127.0.0.1:8000/docs

## Example demo

MapLibre + [OpenFreeMap Liberty](https://tiles.openfreemap.org/styles/liberty), default pitch 30°. The map shows only the **extent** of the downloaded road extract (not the roads). Search for an address or coordinates, or click the map, then compute a single isochrone. Use **Bulk** in the top-left title card to type or import several origins (optional `id` column; blanks become `1`, `2`, …). Shared travel parameters apply to every point; overlapping layers can be toggled per origin. Bulk requests stream results over NDJSON so polygons appear on the map as each origin completes.

The search box plugin is selected by `GEOCODER_PLUGIN` in `.env` (default `photon`: Komoot Photon via `maplibre-gl-geocoder`, free, no API key). Other values: `nominatim`, `stadia`, `maptiler`.

```bash
docker compose up --build
```

- Demo: http://127.0.0.1:8080
- API: http://127.0.0.1:8000

Compose mounts `./data` into the API container and reads the same `.env`. Nginx in the `demo` service proxies `/api/` to FastAPI.

## Tests

```bash
uv run pytest
```

Unit tests use a synthetic grid graph and do not download OSM data.
