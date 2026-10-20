"""Home Assistant MCP server — thin wrapper over HA's REST API.

Whole-home scope: read any entity, fetch history, and call any service.
Secrets (HA_BASE_URL, HA_TOKEN) are injected at runtime via Doppler — never
hardcoded. Built to talk to the Pecron F5000LFP (and everything else in HA)
without SSH-ing through the box each time.
"""
import os
import re
import shlex
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server.fastmcp import FastMCP

HA_URL = os.environ["HA_BASE_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

# Optional: command prefix to invoke pecron-monitor for reliable cloud-REST control
# of the Pecron's own settings (charge limit/speed) — the path that actually works
# (see docs/pecron-api-quirks.md). Examples:
#   "docker exec pecron-monitor-pecron-monitor-1 pecron-monitor --config /config/config.yaml"
#   "pecron-monitor"
# Leave unset to disable the pecron_set_*/pecron_get_settings tools.
PECRON_CMD = os.environ.get("PECRON_MONITOR_CMD", "").strip()

mcp = FastMCP("home-assistant")
client = httpx.Client(
    base_url=HA_URL,
    headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
    timeout=20,
)


def _pecron(*args, timeout=160):
    if not PECRON_CMD:
        raise RuntimeError("PECRON_MONITOR_CMD not set — Pecron control tools are disabled")
    out = subprocess.run(
        shlex.split(PECRON_CMD) + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    return out.stdout + out.stderr


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


@mcp.tool()
def pecron_get_settings() -> dict:
    """Read the Pecron's charge settings over the reliable cloud-REST path.

    Surfaces the values local-TCP/MQTT telemetry hides: ac_charge_stop_value_iaos
    (AC charge limit %), ac_charging_power (speed %), upper/lower_discharge_limit_us,
    eco_quite_mode_as, ac_nor_open_lab, and lifetime PV/grid-charge energy counters.
    Requires PECRON_MONITOR_CMD to be set. See docs/pecron-api-quirks.md.
    """
    blob = _pecron("--raw", "--rest-only")
    codes = [
        "ac_charge_stop_value_iaos", "ac_charging_power", "upper_discharge_limit_us",
        "lower_discharge_limit_us", "eco_quite_mode_as", "ac_nor_open_lab",
        "total_energy", "total_ac_charge_energy",
    ]
    out = {}
    for c in codes:
        m = re.search(rf"{re.escape(c)}[\"\s:]+([0-9]+(?:\.[0-9]+)?|true|false)", blob)
        out[c] = m.group(1) if m else None
    return out


@mcp.tool()
def pecron_set_charge_limit(percent: int) -> dict:
    """Set the AC charge limit (SOC %, firmware min 50) over cloud REST.

    Grid charges the battery up to this SOC then passes through; solar fills above
    it. This is the reliable charge lever — local writes silently fail. The write
    propagates over a few minutes; re-read with pecron_get_settings() to verify.
    """
    if percent < 50 or percent > 100:
        return {"ok": False, "error": "limit must be 50-100 (firmware minimum is 50)"}
    out = _pecron("--control", "ac_charge_stop_value_iaos", str(percent), "--rest-only")
    ok = "CLOUD REST" in out or "Sent" in out
    return {"ok": ok, "requested": percent, "verify": "re-read pecron_get_settings() after ~5 min"}


@mcp.tool()
def pecron_set_charge_speed(percent: int) -> dict:
    """Set the AC charge speed (0-100%, rounded to nearest 10%) over cloud REST.

    Lower it to keep grid charge draw under a grid-input smart plug's safe current.
    Note: on some firmware the cap only binds when set below the natural charge draw.
    """
    enum_val = max(0, min(10, round(percent / 10)))
    out = _pecron("--control", "ac_charging_power_ios", str(enum_val), "--rest-only")
    ok = "CLOUD REST" in out or "Sent" in out
    return {"ok": ok, "requested_pct": percent, "enum": enum_val}


if __name__ == "__main__":
    mcp.run()
