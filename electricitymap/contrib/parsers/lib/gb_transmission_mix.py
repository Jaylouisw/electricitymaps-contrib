"""Shared helper: GB transmission generation mix, by Electricity Maps mode.

Used by the GB distribution-region parsers, which measure consumption and embedded generation
locally but must attribute the transmission-supplied share of demand to something. Elexon's
FUELINST stream is keyless and five-minute granularity; interconnectors are excluded because the
transmission-supplied share is attributed to GB's domestic mix rather than to imported electricity.
"""

from logging import Logger

from requests import Response, Session

ELEXON_STREAM = "https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELINST/stream"
ELEXON_SOURCE = "elexon.co.uk"

ELEXON_TO_MODE = {
    "BIOMASS": "biomass",
    "CCGT": "gas",
    "OCGT": "gas",
    "COAL": "coal",
    "NUCLEAR": "nuclear",
    "WIND": "wind",
    "NPSHYD": "hydro",
    "OIL": "oil",
    "OTHER": "unknown",
}
INTERCONNECTOR_PREFIX = "INT"
# Pumped storage is a consumer while pumping, which Elexon reports as a negative generation.
# It is storage, not a generation mode, so it must not enter the mix or the share denominator.
STORAGE_FUEL_TYPES = {"PS"}


def fetch_transmission_mix(session: Session, logger: Logger | None = None) -> dict[str, float]:
    """GB domestic generation in MW by mode, for the newest interval Elexon publishes."""
    response: Response = session.get(ELEXON_STREAM, params={"format": "json"}, timeout=60)
    if not response.ok:
        raise RuntimeError(f"Elexon request failed: {response.status_code}")
    rows = response.json()
    newest = max(row["startTime"] for row in rows)
    mix: dict[str, float] = {}
    for row in rows:
        fuel_type = row["fuelType"]
        if row["startTime"] != newest:
            continue
        if fuel_type.startswith(INTERCONNECTOR_PREFIX) or fuel_type in STORAGE_FUEL_TYPES:
            continue
        generation = float(row["generation"])
        if generation <= 0:
            # A negative reading is a flow measure (pumping, or a meter convention), not
            # generation; including it would shrink the denominator and inflate every share.
            continue
        mode = ELEXON_TO_MODE.get(fuel_type, "unknown")
        mix[mode] = mix.get(mode, 0.0) + generation
    if not mix:
        raise RuntimeError("Elexon returned no generation rows")
    return mix


def shares(mix: dict[str, float]) -> dict[str, float]:
    total = sum(mix.values())
    if not total:
        raise RuntimeError("Empty transmission mix")
    return {mode: mw / total for mode, mw in mix.items()}
