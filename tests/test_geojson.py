from shapely.geometry import Point, box

from oisochron.geojson import feature, feature_collection, geometry_to_geojson


def test_geometry_to_geojson_point() -> None:
    payload = geometry_to_geojson(Point(9.19, 45.46))
    assert payload["type"] == "Point"
    assert payload["coordinates"] == [9.19, 45.46]


def test_feature_collection_metadata() -> None:
    poly = box(9.0, 45.0, 9.2, 45.2)
    collection = feature_collection(
        [feature(poly, {"kind": "isochrone"})],
        metadata={"travel_type": "foot"},
    )
    assert collection["type"] == "FeatureCollection"
    assert collection["metadata"]["travel_type"] == "foot"
    assert collection["features"][0]["properties"]["kind"] == "isochrone"
    assert collection["features"][0]["geometry"]["type"] == "Polygon"
