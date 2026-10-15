#!/usr/bin/env python3
"""Parser for Northern Powergrid's regional (DNO licence area) data.

Coverage: the North East licence area (GSP group `_F`), zone key `GB-NPG-NE`.
Yorkshire (`_M`, `GB-NPG-Y`) is metered by the same operator but NPG publishes no regional
generation mix for it, and the metering datasets carry site names with no licence-area field,
so attributing sites to the zone needs a site lookup first — that zone raises for now.

Method (validated against NPG's own published intensity, see electricitymaps-contrib#8891):

    consumption = NPG ``totaldemand``                       (measured)
    production  = NPG embedded generation by technology      (measured)
                + GSP import x GB transmission mix shares    (estimated, Elexon FUELINST)

Attributing the GSP import to the GB national mix is deliberate: the transmission system is
GB's, and the alternative reading (NESO's regional figure for that share) reproduces NPG's
own number but imports a modelled product. See #8891 for the open question.

The 30-day dataset stores generation as negative values while the latest-values dataset
stores the same quantities positive, so magnitudes are taken here. Values are kW.
"""

from datetime import datetime, timezone
from logging import Logger, getLogger

from requests import Response, Session

from electricitymap.contrib.config import ZoneKey
from electricitymap.contrib.lib.models.event_lists import (
    ProductionBreakdownList,
    TotalConsumptionList,
)
from electricitymap.contrib.lib.models.events import (
    EventSourceType,
    ProductionMix,
    StorageMix,
)
from electricitymap.contrib.parsers.lib.exceptions import ParserException
from electricitymap.contrib.parsers.lib.gb_transmission_mix import (
    fetch_transmission_mix,
    shares,
)
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


def fetch_production(
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
    if NPG_REGION_BY_ZONE[zone_key] != "North East":
        raise NotImplementedError(
            f"{PARSER}: NPG publishes no regional generation mix for {zone_key}"
        )

    # measured embedded generation, kW -> MW (the dataset stores these as negative values)
    mix = ProductionMix()
    for column, mode in EMBEDDED_COLUMN_TO_MODE.items():
        mix.add_value(mode, abs(float(row.get(column, 0) or 0)) / 1000)

    # Transmission-supplied share, attributed to the GB national mix. The metered GSP import
    # and the identity (demand - embedded) agree to ~1% at some half hours and diverge by
    # 20-40% at others, so the divergence is logged rather than hidden; see #8891 notes.
    gsp_import_mw = abs(float(row["gspimport"])) / 1000
    demand_mw = float(row["totaldemand"]) / 1000
    implied_supply_mw = demand_mw - sum(
        abs(float(row.get(column, 0) or 0)) / 1000 for column in EMBEDDED_COLUMN_TO_MODE
    )
    if demand_mw and abs(implied_supply_mw - gsp_import_mw) > 0.1 * demand_mw:
        logger.warning(
            f"{PARSER}: metered GSP import {gsp_import_mw:.0f} MW vs demand-embedded "
            f"{implied_supply_mw:.0f} MW for {zone_key} at {row['timestamp']}"
        )
    for mode, share in shares(fetch_transmission_mix(session, logger)).items():
        mix.add_value(mode, gsp_import_mw * share)

    storage = StorageMix()
    battery_net = (float(row.get("bessimport", 0)) - abs(float(row.get("bessexport", 0)))) / 1000
    if battery_net:
        storage.add_value("battery", battery_net)

    production_list = ProductionBreakdownList(logger)
    production_list.append(
        datetime=datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc),
        zoneKey=zone_key,
        production=mix,
        storage=storage,
        source=NPG_SOURCE,
        sourceType=EventSourceType.estimated,
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
        datetime=datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc),
        zoneKey=zone_key,
        consumption=float(row["totaldemand"]) / 1000,
        source=NPG_SOURCE,
        sourceType=EventSourceType.measured,
    )
    return consumption_list.to_list()


if __name__ == "__main__":
    print("fetch_production() ->")
    print(fetch_production())
    print("fetch_consumption() ->")
    print(fetch_consumption())
