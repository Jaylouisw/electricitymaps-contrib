import json
from unittest import mock

import pytest
from requests_mock import GET

from electricitymap.contrib.parsers.GB_NPG import (
    NPG_DATASET,
    fetch_consumption,
    fetch_exchange,
    fetch_production,
)
from electricitymap.contrib.types import ZoneKey

MOCKS = "electricitymap/contrib/parsers/tests/mocks/GB_NPG"


def _json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def npg(requests_mock):
    """A captured half-hourly North East row, with a dummy API key."""
    requests_mock.register_uri(GET, NPG_DATASET, json=_json(f"{MOCKS}/region_mix.json"))
    with mock.patch.dict("os.environ", {"NPG_API_KEY": "test"}):
        yield


def test_production(npg, session, snapshot):
    assert snapshot == fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)


def test_consumption(npg, session, snapshot):
    assert snapshot == fetch_consumption(zone_key=ZoneKey("GB-NPG-NE"), session=session)


def test_exchange(npg, session, snapshot):
    assert snapshot == fetch_exchange(
        zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NPG-NE"), session=session
    )


def test_everything_reported_is_measured(npg, session):
    assert (
        fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)[0]["sourceType"].value
        == "measured"
    )
    assert (
        fetch_consumption(zone_key=ZoneKey("GB-NPG-NE"), session=session)[0]["sourceType"].value
        == "measured"
    )
    assert (
        fetch_exchange(
            zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NPG-NE"), session=session
        )[0]["sourceType"].value
        == "measured"
    )


def test_exchange_direction_follows_the_argument_order(npg, session):
    """netFlow is positive when zone_key1 exports, so GB->area is positive while the area imports."""
    importing = fetch_exchange(
        zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NPG-NE"), session=session
    )[0]["netFlow"]
    exporting = fetch_exchange(
        zone_key1=ZoneKey("GB-NPG-NE"), zone_key2=ZoneKey("GB"), session=session
    )[0]["netFlow"]
    assert importing > 0
    assert exporting == -importing


def test_generation_plus_exchange_matches_demand(npg, session):
    """The dataset's own balance is totaldemand = visiblegen + tx, and all three are measured."""
    production = fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)[0]
    consumption = fetch_consumption(zone_key=ZoneKey("GB-NPG-NE"), session=session)[0]
    net_flow = fetch_exchange(
        zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NPG-NE"), session=session
    )[0]["netFlow"]
    assert sum(production["production"].values()) + net_flow == pytest.approx(
        consumption["consumption"], rel=0.05
    )


def test_yorkshire_is_not_implemented(npg, session):
    """NPG meters Yorkshire but publishes no regional generation mix for it."""
    with pytest.raises(NotImplementedError):
        fetch_production(zone_key=ZoneKey("GB-NPG-Y"), session=session)
    with pytest.raises(NotImplementedError):
        fetch_exchange(zone_key1=ZoneKey("GB"), zone_key2=ZoneKey("GB-NPG-Y"), session=session)


def test_exchange_rejects_pairs_without_gb(npg, session):
    with pytest.raises(NotImplementedError):
        fetch_exchange(
            zone_key1=ZoneKey("GB-NGED-WM"), zone_key2=ZoneKey("GB-NPG-NE"), session=session
        )


def test_embedded_generation_is_positive(npg, session):
    """The dataset stores embedded generation as negative; the mix must come out positive."""
    production = fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)[0]
    assert all(value >= 0 for value in production["production"].values())
