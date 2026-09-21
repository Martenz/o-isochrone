from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import IsochroneRequest, app
from oisochron import IncludeLayer, TravelType

client = TestClient(app)


def _schema_enum(spec: dict, schema: dict) -> list[str]:
    if "enum" in schema:
        return schema["enum"]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return spec["components"]["schemas"][name]["enum"]
    if "items" in schema:
        return _schema_enum(spec, schema["items"])
    raise AssertionError(f"no enum in {schema}")


def test_openapi_travel_type_and_include_are_enums() -> None:
    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["IsochroneRequest"]
    properties = schema["properties"]

    assert spec["components"]["schemas"]["TravelType"]["enum"] == [
        item.value for item in TravelType
    ]
    assert spec["components"]["schemas"]["IncludeLayer"]["enum"] == [
        layer.value for layer in IncludeLayer
    ]
    assert _schema_enum(spec, properties["travel_type"]) == [
        item.value for item in TravelType
    ]
    assert _schema_enum(spec, properties["include"]["items"]) == [
        layer.value for layer in IncludeLayer
    ]
    assert properties["include"]["type"] == "array"
    assert "include" not in schema["required"]
    assert "buffer_meters" not in schema["required"]
    assert "h3_resolution" not in schema["required"]
    assert "travel_type" in schema["required"]

    get_params = {
        param["name"]: param
        for param in spec["paths"]["/isochrone"]["get"]["parameters"]
    }
    assert get_params["travel_type"]["required"] is True
    assert _schema_enum(spec, get_params["travel_type"]["schema"]) == [
        item.value for item in TravelType
    ]
    assert get_params["include"]["required"] is False
    assert _schema_enum(spec, get_params["include"]["schema"]["items"]) == [
        layer.value for layer in IncludeLayer
    ]
    assert get_params["buffer_meters"]["required"] is False
    assert get_params["h3_resolution"]["required"] is False


def test_isochrone_request_accepts_aliases_and_comma_separated_include() -> None:
    payload = IsochroneRequest.model_validate(
        {
            "lat": 45.46,
            "lon": 9.19,
            "travel_time_min": 10,
            "travel_type": "feet",
            "include": "lines,h3",
        }
    )
    assert payload.travel_type is TravelType.foot
    assert payload.include == [IncludeLayer.lines, IncludeLayer.h3]


@patch("api.main.compute_isochrone", return_value={"type": "FeatureCollection", "features": []})
def test_get_isochrone_accepts_aliases_and_include_list(mock_compute) -> None:
    response = client.get(
        "/isochrone",
        params={
            "lat": 45.46,
            "lon": 9.19,
            "travel_time_min": 10,
            "travel_type": "bike",
            "include": "lines,filled_buffer",
        },
    )
    assert response.status_code == 200
    mock_compute.assert_called_once()
    args, kwargs = mock_compute.call_args
    assert args[3] is TravelType.bicycle
    assert kwargs["include"] == [IncludeLayer.lines, IncludeLayer.filled_buffer]


@patch("api.main.compute_isochrones_async")
def test_post_isochrone_batch_assigns_payload(mock_compute) -> None:
    async def _fake(*args, **kwargs):
        return [
            {
                "type": "FeatureCollection",
                "features": [{"properties": {"origin_id": "duomo"}}],
                "metadata": {"origin_id": "duomo"},
            },
            {
                "type": "FeatureCollection",
                "features": [{"properties": {"origin_id": "1"}}],
                "metadata": {"origin_id": "1"},
            },
        ]

    mock_compute.side_effect = _fake
    response = client.post(
        "/isochrone/batch",
        json={
            "origins": [
                {"lat": 45.4642, "lon": 9.19, "id": "duomo"},
                {"lat": 45.4863, "lon": 9.2045},
            ],
            "travel_time_min": 12,
            "travel_type": "feet",
            "include": ["buffered_lines", "lines"],
            "max_concurrency": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["collections"][0]["metadata"]["origin_id"] == "duomo"
    mock_compute.assert_called_once()
    args, kwargs = mock_compute.call_args
    assert args[1] == 12
    assert args[2] is TravelType.foot
    assert kwargs["include"] == [IncludeLayer.buffered_lines, IncludeLayer.lines]
    assert kwargs["max_concurrency"] == 2
    origins = args[0]
    assert origins[0].id == "duomo"
    assert origins[1].id is None
    assert origins[1].lat == 45.4863


def test_post_isochrone_batch_rejects_empty_origins() -> None:
    response = client.post(
        "/isochrone/batch",
        json={"origins": [], "travel_time_min": 10, "travel_type": "foot"},
    )
    assert response.status_code == 422
