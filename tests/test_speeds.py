from oisochrone.speeds import TravelType, highway_allowed, parse_travel_type, speed_kph


def test_travel_type_aliases() -> None:
    assert parse_travel_type("feet") is TravelType.foot
    assert parse_travel_type("bicicle") is TravelType.bicycle
    assert parse_travel_type("bike") is TravelType.bicycle
    assert parse_travel_type("drive") is TravelType.car


def test_unknown_travel_type() -> None:
    try:
        parse_travel_type("train")
    except ValueError as exc:
        assert "train" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_default_speeds() -> None:
    assert speed_kph("foot") == 5
    assert speed_kph("bicycle") == 15
    assert speed_kph("car") == 40


def test_highway_filters() -> None:
    assert highway_allowed("residential", "car")
    assert not highway_allowed("footway", "car")
    assert not highway_allowed("motorway", "foot")
    assert highway_allowed("path", "foot")
    assert not highway_allowed("motorway", "bicycle")
