"""Example REST API around the o-isochrone library."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from oisochrone import (
    IncludeLayer,
    IsochroneError,
    IsochroneOrigin,
    TravelType,
    __version__,
    aiter_isochrones,
    assign_origin_ids,
    compute_isochrone,
    compute_isochrones_async,
    get_network_extent,
    get_settings,
    parse_include,
    parse_travel_type,
)
from oisochrone.graph_cache import preload_graph_cache
from oisochrone.seed import start_graph_seed_background

logger = logging.getLogger(__name__)

_LAYER_HELP = (
    "`lines`: reachable road polylines; "
    "`buffered_lines`: metric corridor around reachable roads (default); "
    "`filled_buffer`: corridor with enclosed interiors filled; "
    "`convex_hull`: outer envelope of the reachable network; "
    "`concave_hull`: tighter envelope; "
    "`h3`: Uber H3 hexagons covering the reachable corridor."
)
_MAX_BATCH_ORIGINS = 25
_MAX_BATCH_CONCURRENCY = 8


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.getLogger("oisochrone").setLevel(logging.INFO)
    logging.getLogger("oisochrone.seed").setLevel(logging.INFO)
    logging.getLogger("oisochrone.network").setLevel(logging.INFO)
    settings = get_settings()
    try:
        loaded = preload_graph_cache(settings)
        if loaded:
            logger.info("Graph tile preload ready (%s tile(s) in memory)", loaded)
    except Exception:
        logger.exception("Graph tile preload failed; continuing without warm cache")
    try:
        if start_graph_seed_background(settings):
            logger.info(
                "Graph tile seed running in background from %s "
                "(modes=%s times=%s car_max=%s)",
                settings.graph_seed_coordinates_path,
                ",".join(settings.graph_seed_modes),
                ",".join(str(t) for t in settings.graph_seed_travel_times_min),
                settings.graph_seed_car_max_travel_time_min,
            )
    except Exception:
        logger.exception("Could not start graph tile seed")
    yield


app = FastAPI(
    title="o-isochrone",
    description=(
        "Compute OSM road-network isochrones as GeoJSON.\n\n"
        "**Travel types:** `foot`, `car`, `bicycle` "
        "(aliases such as `walk`, `feet`, `bike`, `bicicle`, and `drive` are accepted).\n\n"
        f"**Include layers:** {_LAYER_HELP}"
    ),
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "isochrone",
            "description": "Compute reachable-area GeoJSON from an origin, time, and mode.",
        },
        {
            "name": "network",
            "description": "Bounding polygon of the configured OSM road extract.",
        },
        {
            "name": "meta",
            "description": "Health and frontend-safe configuration.",
        },
    ],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class IsochroneRequest(BaseModel):
    """Origin, travel budget, mode, and which GeoJSON layers to return."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lat": 45.4642,
                    "lon": 9.19,
                    "travel_time_min": 15,
                    "travel_type": "foot",
                    "include": ["buffered_lines"],
                },
                {
                    "lat": 45.4642,
                    "lon": 9.19,
                    "travel_time_min": 10,
                    "travel_type": "bicycle",
                    "include": ["lines", "filled_buffer", "h3"],
                    "buffer_meters": 15,
                    "h3_resolution": 10,
                },
            ]
        },
    )

    lat: float = Field(
        ...,
        ge=-90,
        le=90,
        description="Origin latitude in WGS84.",
        examples=[45.4642],
    )
    lon: float = Field(
        ...,
        ge=-180,
        le=180,
        description="Origin longitude in WGS84.",
        examples=[9.19],
    )
    travel_time_min: float = Field(
        ...,
        gt=0,
        description="Maximum travel time in minutes.",
        examples=[15],
    )
    travel_type: TravelType = Field(
        ...,
        description=(
            "Travel mode. Selects average speed (`SPEED_*_KPH`) and the OSM highway "
            "filter. Aliases: `walk` / `walking` / `pedestrian` / `feet` → `foot`; "
            "`drive` / `driving` / `auto` → `car`; `bike` / `bicicle` / `cycling` → `bicycle`."
        ),
        examples=["foot"],
    )
    include: list[IncludeLayer] = Field(
        default=[IncludeLayer.buffered_lines],
        description=(
            "Optional. GeoJSON layers to return. In Swagger pick one or more values; "
            "GET also accepts a comma-separated list. "
            f"{_LAYER_HELP} "
            "Legacy values `both` (`lines`+`filled_buffer`) and `polygon` (`filled_buffer`) "
            "are still accepted. Default: `buffered_lines`."
        ),
        examples=[["buffered_lines"], ["lines", "filled_buffer", "h3"]],
    )
    buffer_meters: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional. Corridor width in meters for `buffered_lines` and `filled_buffer`. "
            "If omitted, defaults to 10."
        ),
        examples=[10],
    )
    h3_resolution: int | None = Field(
        default=None,
        ge=0,
        le=15,
        description=(
            "Optional. Uber H3 resolution (0–15) used when `include` contains `h3`. "
            "If omitted, defaults to 10."
        ),
        examples=[10],
    )
    origin_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "Optional. Identifier stamped on the FeatureCollection metadata and "
            "each feature as `origin_id`. Useful when later merging several results."
        ),
        examples=["duomo"],
    )

    @field_validator("travel_type", mode="before")
    @classmethod
    def _normalize_travel_type(cls, value: object) -> TravelType:
        return parse_travel_type(value)

    @field_validator("include", mode="before")
    @classmethod
    def _normalize_include(cls, value: object) -> list[str]:
        return list(parse_include(value if value is not None else None))

    @field_validator("origin_id", mode="before")
    @classmethod
    def _normalize_origin_id(cls, value: object) -> str | None:
        if value is None:
            return None
        ident = str(value).strip()
        return ident or None


class BatchOrigin(BaseModel):
    """One starting point in a batch isochrone request."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90, le=90, description="Origin latitude in WGS84.")
    lon: float = Field(..., ge=-180, le=180, description="Origin longitude in WGS84.")
    id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "Optional origin identifier. If omitted, the library assigns the next "
            "free incremental id (`1`, `2`, …), skipping any ids already provided."
        ),
        examples=["duomo"],
    )

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> str | None:
        if value is None:
            return None
        ident = str(value).strip()
        return ident or None


class IsochroneBatchRequest(BaseModel):
    """Several origins that share travel-time, mode, and layer options."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "origins": [
                        {"lat": 45.4642, "lon": 9.19, "id": "duomo"},
                        {"lat": 45.4863, "lon": 9.2045},
                        {"lat": 45.4515, "lon": 9.175},
                    ],
                    "travel_time_min": 12,
                    "travel_type": "foot",
                    "include": ["buffered_lines"],
                }
            ]
        },
    )

    origins: list[BatchOrigin] = Field(
        ...,
        min_length=1,
        max_length=_MAX_BATCH_ORIGINS,
        description=(
            f"Starting points (max {_MAX_BATCH_ORIGINS}). Each may include an `id`; "
            "missing ids are filled incrementally."
        ),
    )
    travel_time_min: float = Field(
        ...,
        gt=0,
        description="Maximum travel time in minutes, applied to every origin.",
        examples=[12],
    )
    travel_type: TravelType = Field(
        ...,
        description="Travel mode applied to every origin.",
        examples=["foot"],
    )
    include: list[IncludeLayer] = Field(
        default=[IncludeLayer.buffered_lines],
        description=(
            "GeoJSON layers to return for every origin. "
            f"{_LAYER_HELP}"
        ),
        examples=[["buffered_lines"], ["lines", "filled_buffer"]],
    )
    buffer_meters: float | None = Field(
        default=None,
        ge=0,
        description="Corridor width in meters for buffer layers. Default 10.",
        examples=[10],
    )
    h3_resolution: int | None = Field(
        default=None,
        ge=0,
        le=15,
        description="Uber H3 resolution (0–15) when `include` contains `h3`. Default 10.",
        examples=[10],
    )
    max_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_BATCH_CONCURRENCY,
        description=(
            "Optional cap on parallel origin computations "
            f"(1–{_MAX_BATCH_CONCURRENCY}; library default is 4)."
        ),
        examples=[4],
    )

    @field_validator("travel_type", mode="before")
    @classmethod
    def _normalize_travel_type(cls, value: object) -> TravelType:
        return parse_travel_type(value)

    @field_validator("include", mode="before")
    @classmethod
    def _normalize_include(cls, value: object) -> list[str]:
        return list(parse_include(value if value is not None else None))


class IsochroneBatchResponse(BaseModel):
    """One FeatureCollection per origin, in request order."""

    collections: list[dict[str, Any]]
    count: int


class HealthResponse(BaseModel):
    status: str
    version: str


class PublicConfigResponse(BaseModel):
    geocoder_plugin: str
    geocoder_api_key: str | None = None


def _compute(payload: IsochroneRequest) -> dict[str, Any]:
    try:
        return compute_isochrone(
            payload.lat,
            payload.lon,
            payload.travel_time_min,
            payload.travel_type,
            include=payload.include,
            buffer_meters=payload.buffer_meters,
            h3_resolution=payload.h3_resolution,
            origin_id=payload.origin_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IsochroneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/health", tags=["meta"], summary="Service health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.get("/config", tags=["meta"], summary="Frontend-safe settings")
def public_config() -> PublicConfigResponse:
    """Geocoder plugin and API key from the shared `.env` file."""
    get_settings()
    plugin = os.getenv("GEOCODER_PLUGIN", "photon").strip().lower()
    aliases = {
        "maplibre-gl-geocoder": "photon",
        "mapbox.photon": "photon",
        "maplibre-search-box": "stadia",
        "maptiler-geocoding-control": "maptiler",
    }
    return PublicConfigResponse(
        geocoder_plugin=aliases.get(plugin, plugin),
        geocoder_api_key=os.getenv("GEOCODER_API_KEY") or None,
    )


@app.get("/network/extent", tags=["network"], summary="Road-network extract extent")
def network_extent() -> dict[str, Any]:
    """Bounding polygon of the configured / downloaded road-network extract."""
    return get_network_extent()


_ISOCHRONE_RESPONSES = {
    200: {
        "description": "GeoJSON FeatureCollection with one feature per requested layer.",
        "content": {
            "application/json": {
                "example": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "kind": "buffered_lines",
                                "travel_type": "foot",
                                "travel_time_min": 15,
                                "buffer_meters": 10,
                            },
                            "geometry": {"type": "Polygon", "coordinates": []},
                        }
                    ],
                    "metadata": {
                        "lat": 45.4642,
                        "lon": 9.19,
                        "travel_time_min": 15,
                        "travel_type": "foot",
                        "include": ["buffered_lines"],
                    },
                }
            }
        },
    },
    422: {
        "description": "Invalid parameters, or no reachable roads within the travel time.",
    },
}


@app.get(
    "/isochrone",
    tags=["isochrone"],
    summary="Compute an isochrone",
    response_description="GeoJSON FeatureCollection of the requested layers.",
    responses=_ISOCHRONE_RESPONSES,
)
def isochrone_get(payload: Annotated[IsochroneRequest, Query()]) -> dict[str, Any]:
    """Compute a travel-time isochrone from query parameters.

    `travel_type` is one of `foot`, `car`, `bicycle`.
    `include` is optional; pick one or more layers from the list (or pass a
    comma-separated string). `buffer_meters` and `h3_resolution` are optional.
    """
    return _compute(payload)


@app.post(
    "/isochrone",
    tags=["isochrone"],
    summary="Compute an isochrone",
    response_description="GeoJSON FeatureCollection of the requested layers.",
    responses=_ISOCHRONE_RESPONSES,
)
def isochrone_post(payload: IsochroneRequest) -> dict[str, Any]:
    """Compute a travel-time isochrone from a JSON body.

    Same parameters as `GET /isochrone`. `include` is an array of layer names.
    """
    return _compute(payload)


_BATCH_RESPONSES = {
    200: {
        "description": "One GeoJSON FeatureCollection per origin, in request order.",
        "content": {
            "application/json": {
                "example": {
                    "count": 2,
                    "collections": [
                        {
                            "type": "FeatureCollection",
                            "features": [
                                {
                                    "type": "Feature",
                                    "properties": {
                                        "kind": "buffered_lines",
                                        "origin_id": "duomo",
                                    },
                                    "geometry": {"type": "Polygon", "coordinates": []},
                                }
                            ],
                            "metadata": {
                                "lat": 45.4642,
                                "lon": 9.19,
                                "origin_id": "duomo",
                            },
                        }
                    ],
                }
            }
        },
    },
    422: {
        "description": "Invalid parameters, duplicate ids, or no reachable roads for an origin.",
    },
}


@app.post(
    "/isochrone/batch",
    tags=["isochrone"],
    summary="Compute isochrones for many origins",
    response_description="JSON object with one FeatureCollection per origin.",
    responses=_BATCH_RESPONSES,
)
async def isochrone_batch(payload: IsochroneBatchRequest) -> IsochroneBatchResponse:
    """Compute one isochrone per origin, in parallel.

    Shared `travel_time_min`, `travel_type`, `include`, `buffer_meters`, and
    `h3_resolution` apply to every point. Each origin may set `id`; omitted
    ids become `1`, `2`, … (skipping ids already supplied). Every returned
    FeatureCollection and its features include `origin_id` so overlapping
    layers can be filtered per starting point.
    """
    try:
        collections = await compute_isochrones_async(
            _batch_origins(payload),
            payload.travel_time_min,
            payload.travel_type,
            **_batch_kwargs(payload),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IsochroneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IsochroneBatchResponse(collections=collections, count=len(collections))


def _batch_kwargs(payload: IsochroneBatchRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "include": payload.include,
        "buffer_meters": payload.buffer_meters,
        "h3_resolution": payload.h3_resolution,
    }
    if payload.max_concurrency is not None:
        kwargs["max_concurrency"] = payload.max_concurrency
    return kwargs


def _batch_origins(payload: IsochroneBatchRequest) -> list[IsochroneOrigin]:
    return [IsochroneOrigin(lat=item.lat, lon=item.lon, id=item.id) for item in payload.origins]


@app.post(
    "/isochrone/batch/stream",
    tags=["isochrone"],
    summary="Stream isochrones for many origins as each finishes",
    response_description=(
        "Newline-delimited JSON (NDJSON). Events: `start`, `result`, `error`, `done` "
        "(and `fatal` if the batch cannot start)."
    ),
    responses={
        200: {
            "description": "NDJSON event stream (`application/x-ndjson`).",
            "content": {
                "application/x-ndjson": {
                    "example": (
                        '{"type":"start","count":2,"origins":[{"id":"duomo","lat":45.46,"lon":9.19}]}\n'
                        '{"type":"result","index":0,"origin_id":"duomo","collection":{"type":"FeatureCollection"}}\n'
                        '{"type":"done","count":2,"ok":2,"failed":0}\n'
                    )
                }
            },
        },
        422: {"description": "Invalid request body (before the stream starts)."},
    },
)
async def isochrone_batch_stream(payload: IsochroneBatchRequest) -> StreamingResponse:
    """Same inputs as ``POST /isochrone/batch``, but emit one NDJSON line per event.

    Origins are computed in parallel. Each completed origin yields either a
    ``result`` (FeatureCollection) or ``error`` event so clients can paint
    incrementally; a single failure does not abort the rest of the batch.
    """
    try:
        prepared = assign_origin_ids(_batch_origins(payload))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    kwargs = _batch_kwargs(payload)

    async def events() -> AsyncIterator[str]:
        yield json.dumps(
            {
                "type": "start",
                "count": len(prepared),
                "origins": [
                    {"id": origin.id, "lat": origin.lat, "lon": origin.lon}
                    for origin in prepared
                ],
            },
            separators=(",", ":"),
        ) + "\n"
        ok = 0
        failed = 0
        try:
            async for item in aiter_isochrones(
                prepared,
                payload.travel_time_min,
                payload.travel_type,
                **kwargs,
            ):
                if item.ok:
                    ok += 1
                    payload_line = {
                        "type": "result",
                        "index": item.index,
                        "origin_id": item.origin_id,
                        "collection": item.collection,
                    }
                else:
                    failed += 1
                    payload_line = {
                        "type": "error",
                        "index": item.index,
                        "origin_id": item.origin_id,
                        "error": item.error,
                    }
                yield json.dumps(payload_line, separators=(",", ":")) + "\n"
        except ValueError as exc:
            yield json.dumps(
                {"type": "fatal", "error": str(exc)},
                separators=(",", ":"),
            ) + "\n"
            return
        yield json.dumps(
            {"type": "done", "count": len(prepared), "ok": ok, "failed": failed},
            separators=(",", ":"),
        ) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
