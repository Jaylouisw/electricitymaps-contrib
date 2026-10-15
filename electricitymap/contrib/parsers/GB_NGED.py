#!/usr/bin/env python3
"""Parser for National Grid Electricity Distribution's four licence areas.

Zones: GB-NGED-EM (East Midlands), GB-NGED-WM (West Midlands), GB-NGED-SWA (South Wales),
GB-NGED-SW (South West).

Source: NGED's "Live Data" dataset on their CKAN portal — one CSV per licence area, five-minute
cadence, roughly ten minutes behind. Columns: Timestamp, Demand, Generation, Import, Solar, Wind,
STOR, Other (all MW). Two things the data itself does not tell you:

* Timestamps are **local time** (Europe/London), so a 00:40 row is 23:40Z in summer. They are
  converted to UTC here.
* Demand and Import are occasionally blank (seen in East Midlands). Rows with no demand are
  skipped rather than reported as zero.

Method: consumption is measured (Demand), solar and wind are measured, and the Import share is
attributed to the GB transmission mix from Elexon — the open methodology question in
electricitymaps-contrib#8891, which is why those events are marked estimated.

STOR is Short Term Operating Reserve: usually gas or diesel peaking plant, but the feed does not say,
so it is mapped to `unknown` rather than guessed at.
"""

from csv import DictReader
from datetime import datetime, timezone
from io import StringIO
from logging import Logger, getLogger
from zoneinfo import ZoneInfo

from requests import Response, Session

from electricitymap.contrib.config import ZoneKey
from electricitymap.contrib.lib.models.event_lists import (
    ProductionBreakdownList,
    TotalConsumptionList,
)
from electricitymap.contrib.lib.models.events import (
    EventSourceType,
    ProductionMix,
)
from electricitymap.contrib.parsers.lib.exceptions import ParserException
from electricitymap.contrib.parsers.lib.gb_transmission_mix import (
    ELEXON_SOURCE,
    fetch_transmission_mix,
    shares,
)
from electricitymap.contrib.parsers.lib.utils import get_token

PARSER = "GB_NGED.py"
PORTAL = "https://connecteddata.nationalgrid.co.uk"
LIVE_DATA_PACKAGE = f"{PORTAL}/api/3/action/package_show?id=live-data"
SOURCE = "connecteddata.nationalgrid.co.uk"
TZ = ZoneInfo("Europe/London")

# zone key -> CKAN resource name in the "live data" package
ZONE_TO_RESOURCE = {
    ZoneKey("GB-NGED-EM"): "East Midlands",
    ZoneKey("GB-NGED-WM"): "West Midlands",
    ZoneKey("GB-NGED-SWA"): "South Wales",
    ZoneKey("GB-NGED-SW"): "South West",
}

# CSV column -> Electricity Maps mode
COLUMN_TO_MODE = {
    "Solar": "solar",
    "Wind": "wind",
    "STOR": "unknown",
    "Other": "unknown",
}


def _auth_headers() -> dict[str, str]:
    return {"Authorization": get_token("NGED_API_TOKEN")}


def _resource_url(zone_key: ZoneKey, session: Session) -> str:
    """Resolve the CSV for a zone from the package metadata (resource ids can change)."""
    response: Response = session.get(LIVE_DATA_PACKAGE, headers=_auth_headers(), timeout=60)
    if not response.ok:
        raise ParserException(
            parser=PARSER,
            message=f"package_show failed: {response.status_code}",
            zone_key=zone_key,
        )
    wanted = ZONE_TO_RESOURCE[zone_key]
    for resource in response.json()["result"]["resources"]:
        if str(resource.get("name", "")).strip().lower() == wanted.lower():
            return resource["url"]
    raise ParserException(
        parser=PARSER, message=f"no resource named {wanted}", zone_key=zone_key
    )


def _latest_row(zone_key: ZoneKey, session: Session) -> dict[str, str]:
    response: Response = session.get(
        _resource_url(zone_key, session), headers=_auth_headers(), timeout=120
    )
    if not response.ok:
        raise ParserException(
            parser=PARSER,
            message=f"live data download failed: {response.status_code}",
            zone_key=zone_key,
        )
    rows = [row for row in DictReader(StringIO(response.text)) if (row.get("Timestamp") or "").strip()]
    if not rows:
        raise ParserException(parser=PARSER, message="empty live data file", zone_key=zone_key)
    return max(rows, key=lambda row: row["Timestamp"])


def _timestamp(row: dict[str, str]) -> datetime:
    naive = datetime.fromisoformat(row["Timestamp"].strip())
    return naive.replace(tzinfo=TZ).astimezone(timezone.utc)


def _value(row: dict[str, str], column: str) -> float | None:
    raw = (row.get(column) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_production(
    zone_key: ZoneKey = ZoneKey("GB-NGED-EM"),
    session: Session = Session(),
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict]:
    if target_datetime:
        raise NotImplementedError("This parser is not yet able to parse past dates.")
    if zone_key not in ZONE_TO_RESOURCE:
        raise NotImplementedError(f"{PARSER} does not cover {zone_key}")

    row = _latest_row(zone_key, session)
    mix = ProductionMix()
    for column, mode in COLUMN_TO_MODE.items():
        mix.add_value(mode, _value(row, column) or 0.0)

    import_mw = _value(row, "Import")
    source = SOURCE
    if import_mw is not None and import_mw > 0:
        transmission = fetch_transmission_mix(session, logger)
        for mode, share in shares(transmission).items():
            mix.add_value(mode, import_mw * share)
        source = f"{SOURCE}, {ELEXON_SOURCE}"

    production_list = ProductionBreakdownList(logger)
    production_list.append(
        datetime=_timestamp(row),
        zoneKey=zone_key,
        production=mix,
        source=source,
        sourceType=EventSourceType.estimated,
    )
    return production_list.to_list()


def fetch_consumption(
    zone_key: ZoneKey = ZoneKey("GB-NGED-EM"),
    session: Session = Session(),
    target_datetime: datetime | None = None,
    logger: Logger = getLogger(__name__),
) -> list[dict]:
    if target_datetime:
        raise NotImplementedError("This parser is not yet able to parse past dates.")
    if zone_key not in ZONE_TO_RESOURCE:
        raise NotImplementedError(f"{PARSER} does not cover {zone_key}")

    row = _latest_row(zone_key, session)
    demand = _value(row, "Demand")
    if demand is None:
        logger.warning(f"{PARSER}: no demand value for {zone_key} at {row.get('Timestamp')}")
        return []

    consumption_list = TotalConsumptionList(logger)
    consumption_list.append(
        datetime=_timestamp(row),
        zoneKey=zone_key,
        consumption=demand,
        source=SOURCE,
        sourceType=EventSourceType.measured,
    )
    return consumption_list.to_list()


if __name__ == "__main__":
    for zone in ZONE_TO_RESOURCE:
        print(f"{zone} consumption ->", fetch_consumption(ZoneKey(zone)))
        print(f"{zone} production  ->", fetch_production(ZoneKey(zone)))
