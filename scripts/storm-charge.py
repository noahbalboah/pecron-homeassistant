#!/usr/bin/env python3
"""
Pecron storm pre-charge — coordinates with the islander.

Baseline (normal): the islander runs solar self-consumption with AC charging
speed at 0% (grid never charges the battery). Before a storm we WANT the
opposite — fill the battery from grid fast so there's max runtime when the grid
drops.

On a Severe/Extreme NWS alert (Eaton County zone MIC045):
  - drop a flag file the islander watches, so it stands down (no islanding)
  - force grid ON (EP25 plug)
  - set AC charging speed to 80% so grid fast-charges the battery to full
  - re-assert every run while the alert is active (robust to dropped writes)
On clear:
  - restore AC charging speed to 0% (grid pass-through only)
  - remove the flag so the islander resumes solar self-consumption
  - leave the plug on; the islander takes over next run

Run every 5 min with HA_TOKEN exported:
  set -a; . /home/noah/.ha_env; set +a; /usr/bin/python3 /home/noah/.pecron-storm/storm-charge.py
"""
import json
import os
import subprocess
import urllib.request
from datetime import datetime

HA_URL = "http://localhost:8123"
SENSOR = "sensor.nws_severe_weather_alert"
PLUG = "switch.network_server_rack"
CONTAINER = "pecron-monitor-pecron-monitor-1"
SPEED_CTRL = "ac_charging_power_ios"  # enum 0..10 == 0..100%
STORM_SPEED = "8"   # 80%
NORMAL_SPEED = "0"  # 0% (islander baseline — grid never charges)
STORM_FLAG = "/home/noah/.pecron-island/storm_active"
LOG_FILE = "/home/noah/.pecron-storm/storm-charge.log"
INACTIVE = {"none", "unavailable", "unknown", ""}


def now():
    return datetime.now()


def log(msg):
    line = f"{now().isoformat(timespec='seconds')} {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line, end="")


def _token():
    t = os.environ.get("HA_TOKEN")
    if not t:
        raise SystemExit("HA_TOKEN not in env (set -a; . ~/.ha_env; set +a)")
    return t


def ha_get(entity):
    req = urllib.request.Request(
        f"{HA_URL}/api/states/{entity}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("state")


def ha_service(domain_service, data):
    req = urllib.request.Request(
        f"{HA_URL}/api/services/{domain_service}",
        data=json.dumps(data).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def set_speed(value):
    subprocess.run(
        ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
         "/config/config.yaml", "--control", SPEED_CTRL, value],
        check=True, capture_output=True, timeout=150, text=True,
    )


def main():
    state = ha_get(SENSOR)
    active = (state or "").strip().lower() not in INACTIVE
    flag = os.path.exists(STORM_FLAG)

    if active:
        if not flag:
            open(STORM_FLAG, "w").close()
            log(f"STORM '{state}' -> islander yielding, grid ON + charge speed 80% (pre-charge)")
        # re-assert each run while active
        try:
            ha_service("switch/turn_on", {"entity_id": PLUG})
        except Exception as e:
            log(f"grid ON error: {e}")
        try:
            set_speed(STORM_SPEED)
        except Exception as e:
            log(f"set speed 80% error: {e}")
    elif flag:
        # storm just cleared: restore baseline and hand control back to islander
        try:
            set_speed(NORMAL_SPEED)
        except Exception as e:
            log(f"set speed 0% error: {e}")
        try:
            os.remove(STORM_FLAG)
        except OSError:
            pass
        log("storm cleared -> charge speed 0%, islander resumes solar self-consumption")
    # else: clear and no flag -> normal, nothing to do


if __name__ == "__main__":
    main()
