# oisochron

Python library that downloads a **portion** of the OpenStreetMap **road** network with [OSMnx](https://osmnx.readthedocs.io/), then computes a travel-time isochrone.

Given a latitude/longitude, a travel time, and a travel type (`foot`, `car`, `bicycle`), it returns GeoJSON:

- a **polygon** for the reachable area
- a **polyline** (MultiLineString) for the reachable road network

Average speeds come from the shared `.env` file (`SPEED_FOOT_KPH`, `SPEED_BICYCLE_KPH`, `SPEED_CAR_KPH`). Aliases such as `feet` and `bicicle` are accepted.

The FastAPI app and MapLibre demo are **examples** of using the library, not the core product.

## Project layout

```
src/oisochron/     # library (the main product)
api/               # example FastAPI service
demo/              # example MapLibre + OpenFreeMap page (nginx)
scripts/           # Italy PBF download / highway filter
tests/
```

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [osmium-tool](https://osmcode.org/osmium-tool/) if you use a local PBF (`brew install osmium-tool` or `apt install osmium-tool`)
- Docker and Docker Compose to run the example services

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
```

There is no Poetry project file. Dependency resolution is entirely uv.

## Library usage

```python
from oisochron import compute_isochrone, get_network_extent

collection = compute_isochrone(
    lat=45.4642,
    lon=9.1900,
    travel_time_min=15,
    travel_type="foot",   # foot | car | bicycle
    include="both",       # both | polygon | lines
    buffer_meters=None,   # if set, lines become a buffered polygon
)

extent = get_network_extent()  # GeoJSON polygon of the configured extract
```

`include="lines"` plus `buffer_meters=25` returns the reachable roads as a buffered polygon (or MultiPolygon) instead of polylines.

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

That writes `data/italy-roads.osm.pbf` and updates `data/extent.geojson`. OSM data is [ODbL](https://opendatacommons.org/licenses/odbl/).

## Example API

```bash
uv run uvicorn api.main:app --reload --port 8000
```

- `GET /health`
- `GET /network/extent`
- `GET|POST /isochrone` with `lat`, `lon`, `travel_time_min`, `travel_type`
- optional `include=both|polygon|lines`
- optional `buffer_meters` (buffers the reachable roads; returned instead of lines)

OpenAPI: http://127.0.0.1:8000/docs

## Example demo

MapLibre + [OpenFreeMap Liberty](https://tiles.openfreemap.org/styles/liberty), default pitch 30°. The map shows only the **extent** of the downloaded road extract (not the roads). Click to drop a marker, fill the API parameters, and compute. Returned GeoJSON is drawn on the map.

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
