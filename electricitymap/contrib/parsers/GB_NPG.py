#!/usr/bin/env python3
"""Parser for Northern Powergrid's regional (DNO licence area) data.

Coverage: the North East licence area (GSP group `_F`), zone key `GB-NPG-NE`.
Yorkshire (`_M`, `GB-NPG-Y`) is metered by the same operator but NPG publishes no regional
generation mix for it, and the metering datasets carry site names with no licence-area field,
so attributing sites to the zone needs a site lookup first — that zone raises for now.

Everything reported is measured, and the dataset's own balance is

    totaldemand = visiblegen + tx

where `visiblegen` is visible embedded generation and `tx` is the power supplied from the transmission
system. `tx` also equals `gsp_pc` x `totaldemand` exactly, so it is the area's exchange with GB rather
than a derived value. The per-technology columns (`biomass`, `wind`, `pv`, `other`) sum to `visiblegen`
within ~2% across the most recent 100 half-hour rows.

`gspimport` is deliberately unused: across those same rows it disagrees with the balance by a median
19%, so it is not a trustworthy flow field. Those rows are all import-dominated, so the sign convention
for an exporting half hour is unverified; `tx` is passed through signed so an export would show as a
negative net flow.

The 30-day dataset stores generation as negative values (the latest-values dataset stores the same
quantities positive), so magnitudes are taken for generation. Values are kW.
"""

from datetime import datetime, timezone
from logging import Logger, getLogger

from requests import Response, Session

from electricitymap.contrib.config import ZoneKey
from electricitymap.contrib.lib.models.event_lists import (
    ExchangeList,
    ProductionBreakdownList,
    TotalConsumptionList,
)
from electricitymap.contrib.lib.models.events import (
    EventSourceType,
    ProductionMix,
    StorageMix,
)
from electricitymap.contrib.parsers.lib.exceptions import ParserException
from electricitymap.contrib.parsers.lib.utils import get_token

PARSER = "GB_NPG.py"
NPG_PORTAL = "https://northernpowergrid.opendatasoft.com"
NPG_DATASET = f"{NPG_PORTAL}/api/explore/v2.1/catalog/datasets/odp_generation_mix_region_30day/records"
NPG_SOURCE = "northernpowergrid.opendatasoft.com"

NPG_REGION_BY_ZONE: dict[ZoneKey, str] = {
    ZoneKey("GB-NPG-NE"): "North East",
    ZoneKey("GB-NPG-Y"): "Yorkshire",
}

# NPG dataset column -> Electricity Maps mode
EMBEDDED_COLUMN_TO_MODE = {
    "biomass": "biomass",
    "wind": "wind",
    "pv": "solar",
    "other": "unknown",
}


def _fetch_region_mix(zone_key: ZoneKey, session: Session) -> dict:
    region = NPG_REGION_BY_ZONE[zone_key]
    response: Response = session.get(
        NPG_DATASET,
        params={
            "limit": 1,
            "order_by": "timestamp desc",
            "timezone": "UTC",
            "where": f"region='{region}'",
        },
        headers={"Authorization": f"Apikey {get_token('NPG_API_KEY')}"},
    )
    if not response.ok:
        raise ParserException(
            parser=PARSER,
            message=f"Region mix request failed: {response.status_code}",
            zone_key=zone_key,
        )
    results = response.json().get("results", [])
    if not results:
        raise ParserException(
            parser=PARSER,
            message=f"No rows returned for region {region}",
            zone_key=zone_key,
        )
    return results[0]


def _timestamp(row: dict) -> datetime:
    return datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _require_mix(zone_key: ZoneKey) -> None:
    if zone_key not in NPG_REGION_BY_ZONE:
        raise NotImplementedError(f"{PARSER} does not cover {zone_key}")
    if NPG_REGION_BY_ZONE[zone_key] != "North East":
        raise NotImplementedError(
            f"{PARSER}: NPG publishes no regional generation mix for {zone_key}"
        )


def fetch_production(
    zone_key: ZoneKey = ZoneKey("GB-NPG-NE"),
    session: Session = Session(),
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict]:
    if target_datetime:
        raise NotImplementedError("This parser is not yet able to parse past dates.")
    _require_mix(zone_key)

    row = _fetch_region_mix(zone_key, session)

    # measured embedded generation, kW -> MW (the dataset stores these as negative values)
    mix = ProductionMix()
    for column, mode in EMBEDDED_COLUMN_TO_MODE.items():
        mix.add_value(mode, abs(float(row.get(column, 0) or 0)) / 1000)

    storage = StorageMix()
    battery_net = (
        float(row.get("bessimport", 0) or 0) - abs(float(row.get("bessexport", 0) or 0))
    ) / 1000
    if battery_net:
        storage.add_value("battery", battery_net)

    production_list = ProductionBreakdownList(logger)
    production_list.append(
        datetime=_timestamp(row),
        zoneKey=zone_key,
        production=mix,
        storage=storage,
        source=NPG_SOURCE,
        sourceType=EventSourceType.measured,
    )
    return production_list.to_list()


def fetch_consumption(
    zone_key: ZoneKey = ZoneKey("GB-NPG-NE"),
    session: Session = Session(),
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict]:
    if target_datetime:
        raise NotImplementedError("This parser is not yet able to parse past dates.")
    if zone_key not in NPG_REGION_BY_ZONE:
        raise NotImplementedError(f"{PARSER} does not cover {zone_key}")

    row = _fetch_region_mix(zone_key, session)
    consumption_list = TotalConsumptionList(logger)
    consumption_list.append(
        datetime=_timestamp(row),
        zoneKey=zone_key,
        consumption=float(row["totaldemand"]) / 1000,
        source=NPG_SOURCE,
        sourceType=EventSourceType.measured,
    )
    return consumption_list.to_list()


def fetch_exchange(
    zone_key1: ZoneKey = ZoneKey("GB"),
    zone_key2: ZoneKey = ZoneKey("GB-NPG-NE"),
    session: Session = Session(),
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict]:
    """Measured transmission power at the area's grid supply points (`tx`, kW -> MW)."""
    if target_datetime:
        raise NotImplementedError("This parser is not yet able to parse past dates.")
    zones = {zone_key1, zone_key2}
    zone_key = next((z for z in sorted(zones) if z != ZoneKey("GB")), None)
    if len(zones) != 2 or ZoneKey("GB") not in zones or zone_key not in NPG_REGION_BY_ZONE:
        raise NotImplementedError(f"{PARSER} does not cover {zone_key1}->{zone_key2}")
    _require_mix(zone_key)

    row = _fetch_region_mix(zone_key, session)
    # `tx` is positive while the area is supplied from the transmission system, i.e. while GB exports.
    transmission_mw = float(row["tx"]) / 1000
    net_flow = transmission_mw if zone_key1 == ZoneKey("GB") else -transmission_mw

    exchange_list = ExchangeList(logger)
    exchange_list.append(
        datetime=_timestamp(row),
        zoneKey=ZoneKey("->".join(sorted([zone_key1, zone_key2]))),
        netFlow=net_flow,
        source=NPG_SOURCE,
        sourceType=EventSourceType.measured,
    )
    return exchange_list.to_list()


if __name__ == "__main__":
    print("fetch_production() ->")
    print(fetch_production())
    print("fetch_consumption() ->")
    print(fetch_consumption())
    print("fetch_exchange() ->")
    print(fetch_exchange())
