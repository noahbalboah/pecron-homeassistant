#!/usr/bin/env python3
"""
Pecron storm pre-charge — coordinates with the islander.

Baseline (normal): the islander caps grid charging at AC charge limit 50%. Before
a storm we WANT the opposite — fill the battery from grid to 100% so there's max
runtime when the grid drops.

The reliable lever is the AC charge LIMIT (SOC%), written via the CLOUD REST API —
local-TCP writes silently fail, which is what left the limit stuck at 100% before.

On a Severe/Extreme NWS alert (Eaton County zone MIC045):
  - drop a flag file the islander watches, so it stands down (no islanding)
  - force grid ON (EP25 plug)
  - set AC charge limit to 100% so grid charges the battery to full
  - re-assert every run while the alert is active
On clear:
  - set AC charge limit back to 50% via REST and VERIFY via read-back; only remove
    the flag (hand back to the islander) once the limit reads 50% — so the revert
    can never silently fail

Run every 5 min with HA_TOKEN exported:
  set -a; . /home/noah/.ha_env; set +a; /usr/bin/python3 /home/noah/.pecron-storm/storm-charge.py
"""
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime

HA_URL = "http://localhost:8123"
SENSOR = "sensor.nws_severe_weather_alert"
PLUG = "switch.network_server_rack"
CONTAINER = "pecron-monitor-pecron-monitor-1"
LIMIT_CTRL = "ac_charge_stop_value_iaos"  # INT %, the real charge lever
SPEED_CTRL = "ac_charging_power_ios"      # ENUM 0..10 == 0..100% AC charge speed
STORM_SPEED = "6"     # 60% — fill fast but keep grid draw under the EP25 plug's safe current
STORM_LIMIT = "100"   # pre-storm: grid fills the battery to full
NORMAL_LIMIT = 50     # baseline: grid caps at 50%, solar fills the rest
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


def set_limit(value):
    """Set the AC charge limit (SOC%) via the CLOUD REST API (reliable write path)."""
    subprocess.run(
        ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
         "/config/config.yaml", "--control", LIMIT_CTRL, str(value), "--rest-only"],
        check=True, capture_output=True, timeout=150, text=True,
    )


def set_speed(enum_val):
    """Set the AC charge speed via the CLOUD REST API (enum 0..10 == 0..100%)."""
    subprocess.run(
        ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
         "/config/config.yaml", "--control", SPEED_CTRL, str(enum_val), "--rest-only"],
        check=True, capture_output=True, timeout=150, text=True,
    )


def get_limit():
    """Read the AC charge limit (SOC%) via the CLOUD REST API. Returns int or None."""
    try:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
             "/config/config.yaml", "--raw", "--rest-only"],
            check=True, capture_output=True, timeout=150, text=True,
        )
        m = re.search(r"ac_charge_stop_value_iaos[\"\s:]+(\d+)", r.stdout + r.stderr)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def main():
    state = ha_get(SENSOR)
    active = (state or "").strip().lower() not in INACTIVE
    flag = os.path.exists(STORM_FLAG)

    if active:
        if not flag:
            open(STORM_FLAG, "w").close()
            log(f"STORM '{state}' -> islander yielding, grid ON + AC charge limit 100% (pre-charge)")
        # re-assert each run while active
        try:
            ha_service("switch/turn_on", {"entity_id": PLUG})
        except Exception as e:
            log(f"grid ON error: {e}")
        try:
            set_limit(STORM_LIMIT)
        except Exception as e:
            log(f"set limit 100% error: {e}")
        try:
            set_speed(STORM_SPEED)  # cap charge draw under the EP25 plug's safe current
        except Exception as e:
            log(f"set charge speed error: {e}")
    elif flag:
        # storm cleared: restore the 50% cap and hand back to the islander — but ONLY
        # once the limit actually reads 50% (REST writes lag minutes; the old local-TCP
        # write silently failed here, which is what left it stuck at 100%).
        try:
            set_limit(NORMAL_LIMIT)
        except Exception as e:
            log(f"set limit 50% error: {e}")
        cur = get_limit()
        if cur == NORMAL_LIMIT:
            try:
                os.remove(STORM_FLAG)
            except OSError:
                pass
            log("storm cleared -> AC charge limit 50% verified, islander resumes")
        else:
            log(f"storm cleared -> set AC charge limit 50% (reads {cur}, awaiting propagation); "
                f"keeping flag to re-assert next run")
    # else: clear and no flag -> normal, nothing to do


if __name__ == "__main__":
    main()
