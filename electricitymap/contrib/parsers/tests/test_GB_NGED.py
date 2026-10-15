import json
from unittest import mock

import pytest
from requests_mock import GET

from electricitymap.contrib.parsers.GB_NGED import (
    LIVE_DATA_PACKAGE,
    ZONE_TO_RESOURCE,
    fetch_consumption,
    fetch_exchange,
    fetch_production,
)
from electricitymap.contrib.types import ZoneKey

MOCKS = "electricitymap/contrib/parsers/tests/mocks/GB_NGED"


def _package() -> dict:
    return _json(f"{MOCKS}/package_show.json")


def _resource_url(name: str) -> str:
    for resource in _package()["result"]["resources"]:
        if resource["name"] == name:
            return resource["url"]
    raise KeyError(name)


def _json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _csv(name: str) -> bytes:
    with open(f"{MOCKS}/{name}.csv", "rb") as f:
        return f.read()


@pytest.fixture
def nged(requests_mock):
    """Intercept the CKAN metadata and the live CSVs, with a dummy token."""
    requests_mock.register_uri(GET, LIVE_DATA_PACKAGE, json=_package())
    requests_mock.register_uri(GET, _resource_url("West Midlands"), content=_csv("westmidlands"))
    requests_mock.register_uri(GET, _resource_url("East Midlands"), content=_csv("eastmidlands"))
    with mock.patch.dict("os.environ", {"NGED_API_TOKEN": "test"}):
        yield


def test_production_west_midlands(nged, session, snapshot):
    assert snapshot == fetch_production(zone_key=ZoneKey("GB-NGED-WM"), session=session)


def test_consumption_west_midlands(nged, session, snapshot):
    assert snapshot == fetch_consumption(zone_key=ZoneKey("GB-NGED-WM"), session=session)


def test_production_east_midlands(nged, session, snapshot):
    assert snapshot == fetch_production(zone_key=ZoneKey("GB-NGED-EM"), session=session)


def test_blank_demand_yields_no_event(nged, requests_mock, session, caplog):
    """Demand is occasionally blank in these CSVs; that must not become a zero."""
    rows = _csv("eastmidlands").decode("utf-8").splitlines()
    header, *data = rows
    last = data[-1].split(",")
    last[1] = ""
    blanked = "\n".join([header, *data[:-1], ",".join(last)])
    requests_mock.register_uri(GET, _resource_url("East Midlands"), content=blanked.encode())

    assert fetch_consumption(zone_key=ZoneKey("GB-NGED-EM"), session=session) == []
    assert "no demand value" in caplog.text


def test_local_timestamps_are_converted_to_utc(nged, session):
    """The CSVs are in Europe/London; the newest row here is 05:35 local."""
    events = fetch_consumption(zone_key=ZoneKey("GB-NGED-WM"), session=session)
    assert events[0]["datetime"].isoformat() == "2026-10-15T04:35:00+00:00"


def test_exchange_west_midlands(nged, session, snapshot):
    assert snapshot == fetch_exchange(
        zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NGED-WM"), session=session
    )


def test_exchange_direction_follows_the_argument_order(nged, session):
    """netFlow is positive when zone_key1 exports, so GB->area is positive while the area imports."""
    importing = fetch_exchange(
        zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NGED-WM"), session=session
    )[0]["netFlow"]
    exporting = fetch_exchange(
        zone_key1=ZoneKey("GB-NGED-WM"), zone_key2=ZoneKey("GB"), session=session
    )[0]["netFlow"]
    assert importing > 0
    assert exporting == -importing


def test_generation_plus_import_matches_demand(nged, session):
    """NGED's own balance is Demand = Generation + Import, and all three are measured values."""
    production = fetch_production(zone_key=ZoneKey("GB-NGED-WM"), session=session)[0]
    consumption = fetch_consumption(zone_key=ZoneKey("GB-NGED-WM"), session=session)[0]
    net_flow = fetch_exchange(
        zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NGED-WM"), session=session
    )[0]["netFlow"]
    assert sum(production["production"].values()) + net_flow == pytest.approx(
        consumption["consumption"], rel=1e-3
    )


def test_unknown_zone_is_rejected(nged, session):
    with pytest.raises(NotImplementedError):
        fetch_consumption(zone_key=ZoneKey("GB-NPG-NE"), session=session)


def test_exchange_rejects_pairs_without_gb(nged, session):
    with pytest.raises(NotImplementedError):
        fetch_exchange(
            zone_key1=ZoneKey("GB-NGED-WM"), zone_key2=ZoneKey("GB-NGED-EM"), session=session
        )


def test_all_four_zones_are_covered():
    assert set(ZONE_TO_RESOURCE) == {
        ZoneKey("GB-NGED-EM"),
        ZoneKey("GB-NGED-WM"),
        ZoneKey("GB-NGED-SWA"),
        ZoneKey("GB-NGED-SW"),
    }
