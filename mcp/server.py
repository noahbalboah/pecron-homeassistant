"""Home Assistant MCP server — thin wrapper over HA's REST API.

Whole-home scope: read any entity, fetch history, and call any service.
Secrets (HA_BASE_URL, HA_TOKEN) are injected at runtime via Doppler — never
hardcoded. Built to talk to the Pecron F5000LFP (and everything else in HA)
without SSH-ing through the box each time.
"""
import os
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server.fastmcp import FastMCP

HA_URL = os.environ["HA_BASE_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

mcp = FastMCP("home-assistant")
client = httpx.Client(
    base_url=HA_URL,
    headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
    timeout=20,
)


def _states():
    r = client.get("/api/states")
    r.raise_for_status()
    return r.json()


@mcp.tool()
def ha_find(query: str = "", domain: str = "") -> list[dict]:
    """Search Home Assistant entities.

    query: case-insensitive substring matched against entity_id and friendly_name
           (empty = match all).
    domain: optional domain filter, e.g. 'sensor', 'switch', 'climate'.
    Returns a compact list of {entity_id, state, unit, name}.
    """
    q = query.lower()
    out = []
    for s in _states():
        eid = s["entity_id"]
        if domain and not eid.startswith(domain + "."):
            continue
        name = s.get("attributes", {}).get("friendly_name", "")
        if q and q not in eid.lower() and q not in str(name).lower():
            continue
        out.append({
            "entity_id": eid,
            "state": s["state"],
            "unit": s.get("attributes", {}).get("unit_of_measurement", ""),
            "name": name,
        })
    return out


@mcp.tool()
def ha_get(entity_ids: list[str]) -> list[dict]:
    """Get full state + attributes for one or more entity_ids."""
    want = set(entity_ids)
    return [s for s in _states() if s["entity_id"] in want]


@mcp.tool()
def ha_history(entity_id: str, hours: int = 24, max_points: int = 60) -> dict:
    """Recent state history for an entity over the last N hours.

    Downsampled to roughly max_points evenly-spaced samples.
    """
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    r = client.get(
        f"/api/history/period/{start}",
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
    )
    r.raise_for_status()
    data = r.json()
    series = data[0] if data else []
    step = max(1, len(series) // max_points)
    points = [
        {"t": x.get("last_changed") or x.get("last_updated"), "state": x.get("state")}
        for x in series[::step]
    ]
    return {"entity_id": entity_id, "hours": hours, "samples": len(series), "points": points}


@mcp.tool()
def ha_call_service(
    domain: str, service: str, data: dict | None = None, entity_id: str | None = None
) -> list[dict]:
    """Call any Home Assistant service (this is the control path).

    domain/service: e.g. domain='switch', service='turn_on'.
    entity_id: convenience target, e.g. 'switch.pecron_f5000lfp_..._ups_mode'.
    data: any extra service parameters.
    Returns the list of states changed by the call.
    """
    payload = dict(data or {})
    if entity_id:
        payload["entity_id"] = entity_id
    r = client.post(f"/api/services/{domain}/{service}", json=payload)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def pecron_status() -> dict:
    """Key live values for the Pecron F5000LFP power station.

    Bundles SOC, AC/DC input, output power, temperature, and the
    AC/DC/Eco/UPS switches into one compact reading.
    """
    keys = (
        "battery_soc", "ac_input_power", "dc_input_power", "output_power",
        "ac_output_power", "ac_output", "dc_output", "eco_mode", "ups_mode",
        "temperature", "ac_charging_power_setting",
    )
    out = {}
    for s in _states():
        eid = s["entity_id"]
        if "pecron" not in eid.lower():
            continue
        if any(k in eid for k in keys):
            out[eid] = {
                "state": s["state"],
                "unit": s.get("attributes", {}).get("unit_of_measurement", ""),
            }
    return out


if __name__ == "__main__":
    mcp.run()
