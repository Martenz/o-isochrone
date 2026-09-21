# oisochron

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
src/oisochron/     # library (the main product)
api/               # example FastAPI service
demo/              # example MapLibre + OpenFreeMap page (nginx)
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
from oisochron import (
    IsochroneOrigin,
    compute_isochrone,
    compute_isochrones,
    compute_isochrones_async,
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

extent = get_network_extent()  # GeoJSON polygon of the configured extract
```

`include` is a comma-separated (or list) set of layers. Default is `buffered_lines`. Legacy values `both` and `polygon` still work (`both` → `lines,filled_buffer`).

Batch helpers assign incremental string ids (`"1"`, `"2"`, …) when an origin omits `id`, skipping any ids already provided. The async variant `compute_isochrones_async` parallelizes with `asyncio.to_thread` (use it from FastAPI or `await` in Jupyter).

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

If `OSM_SOURCE=pbf` but the file is missing, the library falls back to Overpass so the demo still works.

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

OpenAPI: http://127.0.0.1:8000/docs

## Example demo

MapLibre + [OpenFreeMap Liberty](https://tiles.openfreemap.org/styles/liberty), default pitch 30°. The map shows only the **extent** of the downloaded road extract (not the roads). Search for an address or coordinates, or click the map, then compute a single isochrone. Use **Bulk** in the top-left title card to type or import several origins (optional `id` column; blanks become `1`, `2`, …). Shared travel parameters apply to every point; overlapping layers can be toggled per origin.

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
