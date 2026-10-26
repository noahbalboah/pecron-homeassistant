#!/usr/bin/env python3
"""
Pecron AC charge-limit guard.

Keeps the F5000's AC Charge Limit (ac_charge_stop_value_iaos) at 50%, so grid
only charges the battery to 50% and solar fills the rest. The firmware has
silently reverted this limit to 100% before, and nothing else watches it since
the EP25 islander was retired (2026-10-26).

Reads and writes go through pecron-monitor's cloud REST helpers inside its
container (cloud_api.get_device_properties_rest / set_device_property_rest),
which also holds the account credentials. The helper prints one JSON line.

Firmware quirks (see pecron-monitor docs/known-pecron-api-quirks.md):
  - writes take minutes (up to ~30) to show in a read-back -> wait RETRY_AFTER
    before writing again instead of retrying every run
  - the device rejects a limit below the current SOC -> the write only sticks
    once SOC drops under 50%; until then it is retried every RETRY_AFTER

Run from cron every 15 min:
  /usr/bin/python3 /home/noah/.pecron-limit/pecron-charge-limit.py
Add --dry-run to log decisions without writing.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

CONTAINER = "pecron-monitor-pecron-monitor-1"
LIMIT_CTRL = "ac_charge_stop_value_iaos"
TARGET = 50
RETRY_AFTER = timedelta(minutes=60)
# storm-charge.py owns the limit (raises it to 100%) while this flag exists.
STORM_FLAG = "/home/noah/.pecron-island/storm_active"

STATE_FILE = "/home/noah/.pecron-limit/state.json"
LOG_FILE = "/home/noah/.pecron-limit/guard.log"

DRY = "--dry-run" in sys.argv

# Runs inside the pecron-monitor container. argv[1] = value to write, or "" to read only.
HELPER = r'''
import json, sys, yaml
from cloud_api import login, get_device_properties_rest, set_device_property_rest
from constants import REGIONS

cfg = yaml.safe_load(open("/config/config.yaml"))
region = REGIONS[cfg.get("region", "na")]
dev = cfg["devices"][0]
pk, dk = dev["product_key"], dev["device_key"]
token = login(cfg["email"], cfg["password"], region)["token"]
# getDeviceOnlineStatus answers {} even while the unit reports, so "online"
# means the cloud returned properties (it returns none for an offline unit).
kv = get_device_properties_rest(token, region, pk, dk)
out = {"online": bool(kv)}
if out["online"]:
    out["limit"] = kv.get("ac_charge_stop_value_iaos")
    out["soc"] = kv.get("battery_percentage")
    if sys.argv[1]:
        out["accepted"] = set_device_property_rest(
            token, region, pk, dk, {"ac_charge_stop_value_iaos": int(sys.argv[1])})
print(json.dumps(out))
'''


def now():
    return datetime.now()


def log(msg):
    line = f"{now().isoformat(timespec='seconds')} {'[DRY] ' if DRY else ''}{msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line, end="")


def cloud(write_value=""):
    r = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-", str(write_value)],
        input=HELPER, check=True, capture_output=True, timeout=120, text=True,
    )
    return json.loads(r.stdout.strip().splitlines()[-1])


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def main():
    if os.path.exists(STORM_FLAG):
        log("storm mode active -> yielding to storm-charge")
        return
    st = load_state()
    try:
        cur = cloud()
    except Exception as e:
        log(f"WARN cloud read failed: {e}")
        return
    if not cur["online"]:
        log("Pecron offline -> nothing to check")
        return
    limit, soc = cur.get("limit"), cur.get("soc")
    if limit is None:
        log(f"AC charge limit unreadable (soc={soc}) -> skip")
        return
    if int(limit) == TARGET:
        if st.get("last_write"):
            log(f"AC charge limit = {TARGET}% (verified after re-apply, soc={soc})")
            st.pop("last_write")
            save_state(st)
        return
    last = st.get("last_write")
    if last and now() - datetime.fromisoformat(last) < RETRY_AFTER:
        log(f"AC charge limit = {limit}% (soc={soc}), re-applied {last} -> waiting for it to propagate")
        return
    if DRY:
        log(f"AC charge limit = {limit}% (soc={soc}) -> would re-apply {TARGET}%")
        return
    try:
        res = cloud(TARGET)
    except Exception as e:
        log(f"WARN cloud write failed: {e}")
        return
    log(f"AC charge limit = {limit}% (soc={soc}) -> re-applied {TARGET}% (cloud accepted={res.get('accepted')})")
    st["last_write"] = now().isoformat()
    save_state(st)


if __name__ == "__main__":
    main()
