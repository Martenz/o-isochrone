"""Example REST API around the oisochron library."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from oisochron import (
    IsochroneError,
    __version__,
    compute_isochrone,
    get_network_extent,
    get_settings,
    parse_include,
    parse_travel_type,
)

app = FastAPI(
    title="oisochron",
    description="Compute OSM road-network isochrones as GeoJSON.",
    version=__version__,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class IsochroneRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    travel_time_min: float = Field(..., gt=0)
    travel_type: str = Field(..., examples=["foot", "car", "bicycle"])
    include: list[str] = Field(
        default_factory=lambda: ["buffered_lines"],
        examples=["buffered_lines", "lines,filled_buffer,h3"],
    )
    buffer_meters: float | None = Field(default=None, ge=0)
    h3_resolution: int | None = Field(default=None, ge=0, le=15)

    @field_validator("include", mode="before")
    @classmethod
    def _normalize_include(cls, value: object) -> list[str]:
        return list(parse_include(value if value is not None else None))


def _compute(payload: IsochroneRequest) -> dict[str, Any]:
    try:
        parse_travel_type(payload.travel_type)
        return compute_isochrone(
            payload.lat,
            payload.lon,
            payload.travel_time_min,
            payload.travel_type,
            include=payload.include,
            buffer_meters=payload.buffer_meters,
            h3_resolution=payload.h3_resolution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IsochroneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/config")
def public_config() -> dict[str, str | None]:
    """Frontend-safe settings from the shared .env file."""
    get_settings()
    plugin = os.getenv("GEOCODER_PLUGIN", "photon").strip().lower()
    aliases = {
        "maplibre-gl-geocoder": "photon",
        "mapbox.photon": "photon",
        "maplibre-search-box": "stadia",
        "maptiler-geocoding-control": "maptiler",
    }
    return {
        "geocoder_plugin": aliases.get(plugin, plugin),
        "geocoder_api_key": os.getenv("GEOCODER_API_KEY") or None,
    }


@app.get("/network/extent")
def network_extent() -> dict[str, Any]:
    """Bounding polygon of the configured / downloaded road-network extract."""
    return get_network_extent()


@app.get("/isochrone")
def isochrone_get(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    travel_time_min: float = Query(..., gt=0),
    travel_type: str = Query(..., examples=["foot", "car", "bicycle"]),
    include: str = Query(
        "buffered_lines",
        description=(
            "Comma-separated layers: lines, buffered_lines, filled_buffer, "
            "convex_hull, concave_hull, h3"
        ),
    ),
    buffer_meters: float | None = Query(default=None, ge=0),
    h3_resolution: int | None = Query(default=None, ge=0, le=15),
) -> dict[str, Any]:
    return _compute(
        IsochroneRequest(
            lat=lat,
            lon=lon,
            travel_time_min=travel_time_min,
            travel_type=travel_type,
            include=include,
            buffer_meters=buffer_meters,
            h3_resolution=h3_resolution,
        )
    )


@app.post("/isochrone")
def isochrone_post(payload: IsochroneRequest) -> dict[str, Any]:
    return _compute(payload)
