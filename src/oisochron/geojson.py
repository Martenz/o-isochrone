"""GeoJSON helpers for isochrone polygons and reachable road geometries."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from shapely import to_geojson
from shapely.geometry.base import BaseGeometry


def geometry_to_geojson(geom: BaseGeometry) -> dict[str, Any]:
    """Convert a Shapely geometry to a GeoJSON geometry object."""
    return json.loads(to_geojson(geom))


def feature(
    geom: BaseGeometry,
    properties: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": dict(properties or {}),
        "geometry": geometry_to_geojson(geom),
    }


def feature_collection(
    features: Iterable[dict[str, Any]],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": list(features),
    }
    if metadata:
        payload["metadata"] = dict(metadata)
    return payload
