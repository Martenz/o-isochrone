"""Example REST API around the oisochron library."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from oisochron import (
    IsochroneError,
    __version__,
    compute_isochrone,
    get_network_extent,
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

Include = Literal["both", "polygon", "lines"]


class IsochroneRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    travel_time_min: float = Field(..., gt=0)
    travel_type: str = Field(..., examples=["foot", "car", "bicycle"])
    include: Include = "both"
    buffer_meters: float | None = Field(default=None, ge=0)


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
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IsochroneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


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
    include: Include = Query("both"),
    buffer_meters: float | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    return _compute(
        IsochroneRequest(
            lat=lat,
            lon=lon,
            travel_time_min=travel_time_min,
            travel_type=travel_type,
            include=include,
            buffer_meters=buffer_meters,
        )
    )


@app.post("/isochrone")
def isochrone_post(payload: IsochroneRequest) -> dict[str, Any]:
    return _compute(payload)
