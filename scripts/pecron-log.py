#!/usr/bin/env python3
"""
Pecron clean-day logger — read-only.

Appends a CSV row every run with the signals needed to decide whether a
nightly island-and-discharge scheme (use stored solar overnight instead of
grid) is worth building:

  ts             timestamp (local)
  soc            battery SOC %                         -> how long pinned at 100% (surplus window)
  grid_w         EP25 grid draw into Pecron (W)        -> reliable; charging + passthrough
  pv_w           Pecron DC/PV input (W)                -> live solar
  load_w         Pecron AC output (W)                  -> homelab load on the Pecron
  grid_kwh_today EP25 cumulative grid energy today     -> the kWh a night-discharge could offset

Run from cron every 5 min with the HA token in the environment:
  . /home/noah/.ha_env && /usr/bin/python3 /home/noah/.pecron-log/pecron-log.py
(use `set -a` before sourcing so HA_TOKEN is exported to this process)
"""
import csv
import json
import os
import urllib.request
from datetime import datetime

HA_URL = "http://localhost:8123"
CSV_PATH = "/home/noah/.pecron-log/pecron-log.csv"
SENSORS = {
    "soc": "sensor.pecron_f5000lfp_3c1acca326f2_battery_soc",
    "grid_w": "sensor.network_server_rack_current_consumption",
    "pv_w": "sensor.pecron_f5000lfp_3c1acca326f2_dc_input_power",
    "load_w": "sensor.pecron_f5000lfp_3c1acca326f2_output_power",
    "grid_kwh_today": "sensor.network_server_rack_today_s_consumption",
}


def get_state(entity, token):
    req = urllib.request.Request(
        f"{HA_URL}/api/states/{entity}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp).get("state", "")


def main():
    token = os.environ.get("HA_TOKEN")
    if not token:
        raise SystemExit("HA_TOKEN not in environment (source ~/.ha_env with set -a)")
    row = {"ts": datetime.now().isoformat(timespec="seconds")}
    for key, entity in SENSORS.items():
        try:
            row[key] = get_state(entity, token)
        except Exception:
            row[key] = "ERR"
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ts"] + list(SENSORS.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    main()
