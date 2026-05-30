#!/usr/bin/env python3
"""
Pecron solar self-consumption islander.

Maximizes solar self-consumption by islanding the Pecron (cutting grid via the
EP25 "Pecron" plug, switch.network_server_rack) so it runs the homelab on
battery + solar, and reconnecting grid only at a low-SOC floor.

Pecron has NO native battery-priority mode and will NOT reconnect grid on its
own, so THIS SCRIPT IS THE FAILSAFE — it must reliably restore grid at the floor
(backed only by the downstream CyberPower UPS catching a cutout with a clean
shutdown). Baseline config assumed: AC Charging Speed 0% (grid never charges the
battery), AC Charge Limit 100% (solar can fill).

Run from cron every 3 min with HA_TOKEN exported:
  set -a; . /home/noah/.ha_env; set +a; /usr/bin/python3 /home/noah/.pecron-island/pecron-island.py
Add --dry-run to log decisions without toggling anything.

Logic each run:
  SOC <= FLOOR (25)                  -> GRID ON  (failsafe; overrides dwell; alert if it can't switch)
  SOC >= RESUME (40) and solar>=load -> ISLAND   (grid OFF; respects min-dwell)
  else                               -> hold current state
  SOC unreadable while islanded      -> GRID ON  (can't see the battery -> fail safe) + alert
  SOC <= CRITICAL (15)               -> urgent iPhone alert
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

HA_URL = "http://localhost:8123"
PLUG = "switch.network_server_rack"
S_SOC = "sensor.pecron_f5000lfp_3c1acca326f2_battery_soc"
S_PV = "sensor.pecron_f5000lfp_3c1acca326f2_dc_input_power"
S_LOAD = "sensor.pecron_f5000lfp_3c1acca326f2_output_power"
S_GRIDW = "sensor.network_server_rack_current_consumption"
S_SPEED = "sensor.pecron_f5000lfp_3c1acca326f2_ac_charging_power_setting"
NOTIFY = "notify/mobile_app_noahs_iphone"

# Pecron resets AC charging speed to 100% whenever AC input is cut (i.e. every
# island). We must force it back to 0% on each grid-on so grid passes through
# WITHOUT charging the battery (solar stays the only charger).
CONTAINER = "pecron-monitor-pecron-monitor-1"
SPEED_CTRL = "ac_charging_power_ios"  # enum 0..10 == 0..100%
STORM_FLAG = "/home/noah/.pecron-island/storm_active"

FLOOR = 25          # SOC% at/below which grid is forced on (failsafe)
RESUME = 40         # SOC% at/above which we island again (with solar>=load)
CRITICAL = 15       # SOC% for urgent alert
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


def set_charge_speed_off():
    """Force AC charging speed to 0% via the pecron CLI (grid pass-through, no charge)."""
    if DRY:
        log("(DRY) would set AC charging speed 0%")
        return
    try:
        subprocess.run(
            ["docker", "exec", CONTAINER, "pecron-monitor", "--config",
             "/config/config.yaml", "--control", SPEED_CTRL, "0"],
            check=True, capture_output=True, timeout=150, text=True,
        )
        log("set AC charging speed 0% (grid pass-through, battery not charged from grid)")
    except Exception as e:
        log(f"WARN could not set charging speed 0%: {e}")


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
        speed = num(ha_get(S_SPEED))
    except Exception as e:
        log(f"FATAL read error: {e}")
        return
    cur = "on" if sw == "on" else "off"
    log(f"soc={soc} pv={pv} load={load} grid_switch={cur} grid_w={gridw} chg_speed={speed}")

    # During a storm, storm-charge owns the plug + charging — stand down.
    if os.path.exists(STORM_FLAG):
        log("storm mode active -> yielding to storm-charge")
        return

    # SOC unreadable -> fail safe (can't see the battery, don't risk islanding)
    if soc is None:
        if cur != "on":
            log("SOC unreadable while islanded -> forcing GRID ON")
            if not set_grid("on", st):
                notify("⚠️ Pecron grid restore FAILED",
                       "SOC unreadable and could not turn grid on. Check the homelab.", st)
        else:
            log("SOC unreadable but grid already on -> safe, hold")
        save_state(st)
        return

    # FAILSAFE: at/below floor, make sure grid is actually FLOWING (not just switch=on)
    if soc <= FLOOR:
        flowing = (gridw is not None and gridw > GRID_FLOW_W) or (gridw is None and cur == "on")
        if not flowing:
            log(f"FAILSAFE: SOC {soc:.0f}% <= {FLOOR}% and grid not flowing -> GRID ON")
            if set_grid("on", st):
                set_charge_speed_off()  # grid passes through but must NOT charge the battery
            else:
                notify("⚠️ Pecron grid restore FAILED",
                       f"Battery {soc:.0f}% and could NOT switch grid on after retries. "
                       f"Homelab may shut down — check now.", st)
        else:
            log(f"SOC {soc:.0f}% <= floor but grid already flowing ({gridw}W) -> hold")
            if speed is not None and speed != 0:
                log(f"grid flowing but charge speed {speed:.0f}% != 0 -> correcting (no grid charging)")
                set_charge_speed_off()
        if soc <= CRITICAL:
            notify("⚠️ Pecron battery critically low",
                   f"Battery at {soc:.0f}% (cutoff 5%). Grid {'on' if st.get('grid')=='on' else 'NOT confirmed'}.", st)
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
