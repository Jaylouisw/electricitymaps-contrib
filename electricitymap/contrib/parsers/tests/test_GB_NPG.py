import json
from unittest import mock

import pytest
from requests_mock import GET

from electricitymap.contrib.parsers.GB_NPG import (
    NPG_DATASET,
    fetch_consumption,
    fetch_production,
)
from electricitymap.contrib.parsers.lib.gb_transmission_mix import ELEXON_STREAM
from electricitymap.contrib.types import ZoneKey

MOCKS = "electricitymap/contrib/parsers/tests/mocks/GB_NPG"


def _json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def npg(requests_mock):
    """One half-hourly North East row plus the GB transmission mix, with a dummy token."""
    requests_mock.register_uri(GET, NPG_DATASET, json=_json(f"{MOCKS}/region_mix.json"))
    requests_mock.register_uri(GET, ELEXON_STREAM, json=_json(f"{MOCKS}/fuelinst.json"))
    with mock.patch.dict("os.environ", {"NPG_API_KEY": "test"}):
        yield


def test_production(npg, session, snapshot):
    assert snapshot == fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)


def test_consumption(npg, session, snapshot):
    assert snapshot == fetch_consumption(zone_key=ZoneKey("GB-NPG-NE"), session=session)


def test_consumption_is_measured_production_is_estimated(npg, session):
    production = fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)
    consumption = fetch_consumption(zone_key=ZoneKey("GB-NPG-NE"), session=session)
    assert production[0]["sourceType"].value == "estimated"
    assert consumption[0]["sourceType"].value == "measured"


def test_yorkshire_is_not_implemented(npg, session):
    """NPG meters Yorkshire but publishes no regional generation mix for it."""
    with pytest.raises(NotImplementedError):
        fetch_production(zone_key=ZoneKey("GB-NPG-Y"), session=session)


def test_embedded_generation_is_positive(npg, session):
    """The dataset stores embedded generation as negative; the mix must come out positive."""
    production = fetch_production(zone_key=ZoneKey("GB-NPG-NE"), session=session)[0]
    assert all(value >= 0 for value in production["production"].values())
