#!/usr/bin/env python3
"""
Pecron solar self-consumption islander.

Maximizes solar self-consumption by islanding the Pecron (cutting grid via the
EP25 "Pecron" plug, switch.network_server_rack) so it runs the homelab on
battery + solar, and reconnecting grid only at a low-SOC floor.

Pecron has NO native battery-priority mode and will NOT reconnect grid on its
own, so THIS SCRIPT IS THE FAILSAFE — it must reliably restore grid at the floor
(backed only by the downstream CyberPower UPS catching a cutout with a clean
shutdown).

Charge policy: AC Charge Limit = 50% (set in the Pecron app; firmware-enforced) is
the everyday lever — grid tops the battery to 50% then holds via passthrough, solar
fills 50->100%. Because that limit is UNRELIABLE on this firmware (it can silently
revert to 100%, e.g. after a storm pre-charge), this script also runs a PLUG SAFETY
NET: if grid is actively charging the battery past ~55%, the limit has reverted ->
re-assert 50% (best-effort) AND island to physically stop the grid fill.

Run from cron every 3 min with HA_TOKEN exported:
  set -a; . /home/noah/.ha_env; set +a; /usr/bin/python3 /home/noah/.pecron-island/pecron-island.py
Add --dry-run to log decisions without toggling anything.

Logic each run:
  SOC <= FLOOR (25)                  -> GRID ON + REST-set AC charge limit 50% (low SOC = device accepts it)
  grid charging & SOC >= CAP (55)    -> ISLAND (charge limit reverted; plug is the reliable stop)
  SOC >= RESUME (40) and solar>=load -> ISLAND   (grid OFF; respects min-dwell)
  else                               -> hold current state
  SOC unreadable while islanded      -> GRID ON  (can't see the battery -> fail safe) + alert
  SOC <= CRITICAL (15)               -> urgent iPhone alert
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HA_URL = "http://localhost:8123"
PLUG = "switch.network_server_rack"
S_SOC = "sensor.pecron_f5000lfp_3c1acca326f2_battery_soc"
S_PV = "sensor.pecron_f5000lfp_3c1acca326f2_dc_input_power"
S_LOAD = "sensor.pecron_f5000lfp_3c1acca326f2_output_power"
S_GRIDW = "sensor.network_server_rack_current_consumption"
NOTIFY = "notify/mobile_app_noahs_iphone"

# The reliable charge lever is the AC charge LIMIT (SOC%), read+written via the
# CLOUD REST API — local-TCP/MQTT writes silently don't stick. KEY FIRMWARE RULE:
# the device REJECTS setting the limit below the current SOC, so we only assert it
# at the floor (low SOC) where it WILL take, then read it back to verify.
CONTAINER = "pecron-monitor-pecron-monitor-1"
LIMIT_CTRL = "ac_charge_stop_value_iaos"  # INT %, firmware min 50 — the real charge lever
SPEED_CTRL = "ac_charging_power_ios"      # ENUM 0..10 == 0..100% AC charge speed
STORM_FLAG = "/home/noah/.pecron-island/storm_active"

FLOOR = 25          # SOC% at/below which grid is forced on (failsafe)
RESUME = 40         # SOC% at/above which we island again (with solar>=load)
CRITICAL = 15       # SOC% for urgent alert
AC_CHARGE_LIMIT = 50        # SOC% grid charge target (firmware min; also set in app)
SAFE_SPEED_PCT = 60         # cap AC charge speed so grid draw stays under the EP25 plug's safe current
SAFE_SPEED_ENUM = "6"       # ac_charging_power_ios enum for 60%
SAFETY_CAP = 55             # SOC% above which, IF grid is still charging, the limit reverted
CHARGE_DETECT_MARGIN = 300  # EP25 watts above load that means grid is charging the battery
GRID_FLOW_W = 100   # EP25 watts above this == grid actually flowing
MIN_DWELL = timedelta(minutes=15)
ALERT_COOLDOWN = timedelta(minutes=30)

STATE_FILE = "/home/noah/.pecron-island/state.json"
LOG_FILE = "/home/noah/.pecron-island/island.log"

DRY = "--dry-run" in sys.argv


def now():
    return datetime.now()


def log(msg):
    line = f"{now().isoformat(timespec='seconds')} {'[DRY] ' if DRY else ''}{msg}\n"
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
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("state")
    except urllib.error.HTTPError as e:
        # Entity gone (e.g. renamed by a pecron-monitor upgrade) == unreadable,
        # so the SOC-unreadable failsafe runs instead of aborting the whole run.
        if e.code == 404:
            return None
        raise


def ha_service(domain_service, data):
    req = urllib.request.Request(
        f"{HA_URL}/api/services/{domain_service}",
        data=json.dumps(data).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def rest_get_limit():
    """Read the AC charge limit (SOC%) via the CLOUD REST API — the reliable read-back
    (local-TCP/MQTT don't expose it). Returns int or None."""
    try:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
             "/config/config.yaml", "--raw", "--rest-only"],
            check=True, capture_output=True, timeout=150, text=True,
        )
        m = re.search(r"ac_charge_stop_value_iaos[\"\s:]+(\d+)", r.stdout + r.stderr)
        return int(m.group(1)) if m else None
    except Exception as e:
        log(f"WARN REST read of AC charge limit failed: {e}")
        return None


def rest_set_limit(val):
    """Set the AC charge limit (SOC%) via the CLOUD REST API (the reliable write path).
    NOTE: the firmware rejects setting the limit BELOW the current SOC, so only call
    this at low SOC (e.g. the floor). Returns True if the cloud accepted the command."""
    if DRY:
        log(f"(DRY) would REST-set AC charge limit {val}%")
        return False
    try:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
             "/config/config.yaml", "--control", LIMIT_CTRL, str(val), "--rest-only"],
            check=True, capture_output=True, timeout=150, text=True,
        )
        ok = "via CLOUD REST" in (r.stdout + r.stderr)
        log(f"REST-set AC charge limit {val}% (cloud accepted={ok})")
        return ok
    except Exception as e:
        log(f"WARN REST-set AC charge limit {val}% failed: {e}")
        return False


def assert_charge_limit():
    """At low SOC the firmware WILL accept a lower AC charge limit. Read it via REST;
    if it isn't AC_CHARGE_LIMIT, set it (REST) — verified by the read on the next run.
    This is the reliable, verifiable cap; the plug safety-net is the physical backup."""
    cur_limit = rest_get_limit()
    if cur_limit is None:
        log("AC charge limit unreadable via REST -> relying on plug safety-net")
        return
    if cur_limit == AC_CHARGE_LIMIT:
        log(f"AC charge limit = {AC_CHARGE_LIMIT}% (REST-verified) -> grid caps there")
        return
    log(f"AC charge limit = {cur_limit}% (want {AC_CHARGE_LIMIT}%), SOC low -> REST-set")
    rest_set_limit(AC_CHARGE_LIMIT)


def rest_get_speed():
    """Read the AC charge speed (%) via the CLOUD REST API. Returns int or None."""
    try:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
             "/config/config.yaml", "--raw", "--rest-only"],
            check=True, capture_output=True, timeout=150, text=True,
        )
        m = re.search(r"ac_charging_power[\"\s:]+(\d+)", r.stdout + r.stderr)
        return int(m.group(1)) if m else None
    except Exception as e:
        log(f"WARN REST read of AC charge speed failed: {e}")
        return None


def rest_set_speed(enum_val):
    """Set the AC charge speed via the CLOUD REST API (enum 0..10 == 0..100%)."""
    if DRY:
        log(f"(DRY) would REST-set AC charge speed enum {enum_val}")
        return
    try:
        subprocess.run(
            ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
             "/config/config.yaml", "--control", SPEED_CTRL, str(enum_val), "--rest-only"],
            check=True, capture_output=True, timeout=150, text=True,
        )
        log(f"REST-set AC charge speed {SAFE_SPEED_PCT}% (protect EP25 plug from over-current)")
    except Exception as e:
        log(f"WARN REST-set AC charge speed failed: {e}")


def assert_charge_speed():
    """Cap grid charge speed so the draw through the EP25 plug stays under its safe
    current. The firmware resets speed to 100% whenever AC input is cut, so re-assert
    when grid is on at the floor. Only writes when it's above the cap."""
    cur = rest_get_speed()
    if cur is None:
        return
    if cur <= SAFE_SPEED_PCT:
        log(f"AC charge speed = {cur}% (<= {SAFE_SPEED_PCT}% cap) -> ok")
        return
    log(f"AC charge speed = {cur}% > {SAFE_SPEED_PCT}% cap -> throttling")
    rest_set_speed(SAFE_SPEED_ENUM)


def num(state):
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def notify(title, message, st):
    last = parse_ts(st.get("last_alert"))
    if last and now() - last < ALERT_COOLDOWN:
        log(f"(alert suppressed by cooldown) {title}: {message}")
        return
    if DRY:
        log(f"(DRY) would alert -> {title}: {message}")
    else:
        try:
            ha_service(NOTIFY, {"title": title, "message": message})
            log(f"ALERT sent -> {title}: {message}")
        except Exception as e:
            log(f"ALERT FAILED ({e}) -> {title}: {message}")
    st["last_alert"] = now().isoformat()


def set_grid(desired, st):
    """desired 'on' (grid) or 'off' (island). Returns True if verified."""
    service = "switch/turn_on" if desired == "on" else "switch/turn_off"
    for attempt in range(1, 4):
        if DRY:
            log(f"(DRY) would call {service} on {PLUG} (attempt {attempt}) -> assume ok")
            st["grid"] = desired
            st["last_toggle"] = now().isoformat()
            return True
        try:
            ha_service(service, {"entity_id": PLUG})
        except Exception as e:
            log(f"{service} error attempt {attempt}: {e}")
            time.sleep(5)
            continue
        time.sleep(12)
        try:
            sw = ha_get(PLUG)
            gw = num(ha_get(S_GRIDW))
        except Exception as e:
            log(f"verify read error attempt {attempt}: {e}")
            sw, gw = None, None
        ok = (desired == "on" and sw == "on") or (desired == "off" and sw == "off")
        log(f"toggle->{desired} attempt {attempt}: switch={sw} grid_w={gw} ok={ok}")
        if ok:
            st["grid"] = desired
            st["last_toggle"] = now().isoformat()
            return True
        time.sleep(5)
    return False


def main():
    st = load_state()
    try:
        soc = num(ha_get(S_SOC))
        pv = num(ha_get(S_PV))
        load = num(ha_get(S_LOAD))
        sw = ha_get(PLUG)
        gridw = num(ha_get(S_GRIDW))
    except Exception as e:
        log(f"FATAL read error: {e}")
        return
    cur = "on" if sw == "on" else "off"
    log(f"soc={soc} pv={pv} load={load} grid_switch={cur} grid_w={gridw}")

    # Telemetry restored after an offline episode -> clear the flag + notify.
    if soc is not None and st.get("offline"):
        notify("✅ Pecron back online", f"Telemetry restored (SOC {soc:.0f}%).", st)
        st["offline"] = False
        save_state(st)

    # During a storm, storm-charge owns the plug + charging — stand down.
    if os.path.exists(STORM_FLAG):
        log("storm mode active -> yielding to storm-charge")
        return

    # SOC unreadable -> the Pecron is offline/blind (cloud/Wi-Fi down or unit off).
    # Fail safe (force grid on, don't risk islanding) and alert once per episode.
    if soc is None:
        if not st.get("offline"):
            notify("⚠️ Pecron OFFLINE",
                   "No telemetry from the Pecron (cloud/Wi-Fi down or unit off). "
                   "Forcing grid ON as failsafe; control is blind until it returns.", st)
            st["offline"] = True
        if cur != "on":
            log("SOC unreadable (offline) while islanded -> forcing GRID ON")
            if not set_grid("on", st):
                notify("⚠️ Pecron grid restore FAILED",
                       "SOC unreadable and could not turn grid on. Check the homelab.", st)
        else:
            log("SOC unreadable (offline) but grid already on -> safe, hold")
        save_state(st)
        return

    # FAILSAFE: at/below floor, make sure grid is actually FLOWING (not just switch=on)
    if soc <= FLOOR:
        flowing = (gridw is not None and gridw > GRID_FLOW_W) or (gridw is None and cur == "on")
        if not flowing:
            log(f"FAILSAFE: SOC {soc:.0f}% <= {FLOOR}% and grid not flowing -> GRID ON")
            if not set_grid("on", st):
                notify("⚠️ Pecron grid restore FAILED",
                       f"Battery {soc:.0f}% and could NOT switch grid on after retries. "
                       f"Homelab may shut down — check now.", st)
        else:
            log(f"SOC {soc:.0f}% <= floor but grid already flowing ({gridw}W) -> hold")
        # SOC is low here -> the firmware WILL accept a lower AC charge limit now.
        # This is the reliable, verifiable cap: grid tops to 50% then holds, solar fills the rest.
        assert_charge_limit()
        # Cap charge speed too, so the grid draw through the EP25 plug stays under its safe current.
        assert_charge_speed()
        if soc <= CRITICAL:
            notify("⚠️ Pecron battery critically low",
                   f"Battery at {soc:.0f}% (cutoff 5%). Grid {'on' if st.get('grid')=='on' else 'NOT confirmed'}.", st)
        save_state(st)
        return

    # SAFETY NET: grid actively charging the battery past the cap == the 50% AC
    # charge limit has reverted (e.g. storm-charge set 100% and the CLI revert
    # silently failed). Re-assert 50% AND island to physically stop the grid fill.
    # Distinguishes grid-charging (grid_w >> load) from solar-charging (grid_w ~ load),
    # so it never fires when SOLAR is the one filling the battery above 50%.
    charging_from_grid = (
        cur == "on"
        and gridw is not None
        and load is not None
        and gridw > load + CHARGE_DETECT_MARGIN
    )
    if charging_from_grid and soc >= SAFETY_CAP:
        log(f"SAFETY: grid charging past cap (SOC {soc:.0f}% >= {SAFETY_CAP}%, "
            f"grid {gridw:.0f}W >> load {load:.0f}W) -> AC charge limit reverted to 100%")
        # Can't lower the limit now (firmware won't set it below the current SOC) —
        # islanding (cut the plug) is the reliable physical stop. The limit is
        # re-asserted to 50% by assert_charge_limit() at the floor once SOC is low.
        notify("⚠️ Pecron grid-charging past 50%",
               f"Battery {soc:.0f}%, grid pulling {gridw:.0f}W (load {load:.0f}W). "
               f"Charge limit reverted — islanding to stop the grid fill.", st)
        last_toggle = parse_ts(st.get("last_toggle"))
        if last_toggle is not None and now() - last_toggle < MIN_DWELL:
            log("SAFETY island deferred (min-dwell) -> will island next eligible run")
        else:
            set_grid("off", st)
        save_state(st)
        return

    # Above floor: discretionary islanding
    if soc >= RESUME and pv is not None and load is not None and pv >= load:
        desired = "off"
    else:
        desired = cur  # hold

    if desired == cur:
        save_state(st)
        return

    # desired == 'off' (island) — respects min-dwell
    last_toggle = parse_ts(st.get("last_toggle"))
    if last_toggle is not None and now() - last_toggle < MIN_DWELL:
        log(f"want ISLAND but min-dwell not elapsed (last {st.get('last_toggle')}) -> hold")
        save_state(st)
        return
    log(f"decision: ISLAND (SOC {soc:.0f}% >= {RESUME}% and solar {pv:.0f}W >= load {load:.0f}W)")
    set_grid("off", st)
    save_state(st)


if __name__ == "__main__":
    main()
