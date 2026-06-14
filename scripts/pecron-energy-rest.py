#!/usr/bin/env python3
"""
Pecron native energy counters -> Home Assistant (read-only).

The Pecron keeps its own lifetime/session energy totals that the MQTT telemetry
doesn't expose but the CLOUD REST API does (getDeviceBusinessAttributes). These
are the real numbers for solar ROI — actual PV generated vs actual grid charged —
instead of HA's Riemann integration "since tracking start, no backfill".

Reads them via `pecron-monitor --raw --rest-only` and pushes them to HA as
sensors via the HA REST API (states refresh every run; that's fine for slow
counters). Pair with the existing pecron ROI package on the Energy view.

Run from cron every 10 min with HA_TOKEN exported:
  set -a; . /home/noah/.ha_env; set +a; /usr/bin/python3 /home/noah/.pecron-energy/pecron-energy-rest.py
"""
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime

HA_URL = "http://localhost:8123"
CONTAINER = "pecron-monitor-pecron-monitor-1"
LOG_FILE = "/home/noah/.pecron-energy/pecron-energy-rest.log"

# REST property code -> (HA entity suffix, friendly name)
COUNTERS = {
    "total_energy": ("pv_generated_total", "Pecron PV Generated (lifetime)"),
    "total_ac_charge_energy": ("grid_charge_total", "Pecron Grid Charge (lifetime)"),
    "total_ac_output_energy": ("ac_output_total", "Pecron AC Output (lifetime)"),
    "total_dc_output_energy": ("dc_output_total", "Pecron DC Output (lifetime)"),
    "solar_panel_power_generation": ("pv_generated_session", "Pecron PV Generated (session)"),
    "ac_charge_energy": ("grid_charge_session", "Pecron Grid Charge (session)"),
}


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line, end="")


def _token():
    t = os.environ.get("HA_TOKEN")
    if not t:
        raise SystemExit("HA_TOKEN not in env (set -a; . ~/.ha_env; set +a)")
    return t


def read_rest_raw():
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
         "/config/config.yaml", "--raw", "--rest-only"],
        check=True, capture_output=True, timeout=150, text=True,
    )
    return r.stdout + r.stderr


def parse_float(blob, code):
    m = re.search(rf"{re.escape(code)}[\"\s:]+([0-9]+(?:\.[0-9]+)?)", blob)
    return float(m.group(1)) if m else None


def ha_set_state(entity, value, friendly):
    body = json.dumps({
        "state": f"{value:.3f}",
        "attributes": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "friendly_name": friendly,
            "source": "pecron REST getDeviceBusinessAttributes",
        },
    }).encode()
    req = urllib.request.Request(
        f"{HA_URL}/api/states/{entity}",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    try:
        blob = read_rest_raw()
    except Exception as e:
        log(f"FATAL REST read failed: {e}")
        return
    published, missing = [], []
    for code, (suffix, friendly) in COUNTERS.items():
        val = parse_float(blob, code)
        if val is None:
            missing.append(code)
            continue
        try:
            ha_set_state(f"sensor.pecron_native_{suffix}", val, friendly)
            published.append(f"{suffix}={val:.3f}")
        except Exception as e:
            log(f"WARN publish {suffix} failed: {e}")
    log(f"published {len(published)}: {', '.join(published)}"
        + (f" | missing: {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
