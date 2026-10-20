# Pecron F5000LFP — API & control quirks

Reverse-engineered notes for controlling a **Pecron F5000LFP** (and likely the
E3600LFP and siblings on the same cloud platform — Landecia / `iot-api.landecia.com`).
Pecron publishes no official API docs, and the control surface has several
non-obvious traps. Everything below was verified against a live unit.

The `--rest-only` / `--control` / `--raw` commands refer to
[`pecron-monitor`](https://github.com/attractify-logan/pecron-monitor); the
underlying REST endpoints are noted so you can use them from anything.

---

## TL;DR

- **To read settings reliably, use the cloud REST API** (`getDeviceBusinessAttributes`).
  Local-TCP and MQTT telemetry do **not** expose most *settings* (charge limit,
  speed, discharge limits, mode flags).
- **To write settings reliably, use the cloud REST API** (`batchControlDevice`).
  **Local-TCP writes silently fail** — the command "succeeds" but nothing changes.
- **REST writes apply with a multi-minute propagation lag.** Don't conclude a
  write failed from a read-back a few seconds later. Re-read after ~5 minutes.
- **The real grid-charge lever is the AC charge *limit* (SOC %), not the charge
  *speed*.** Speed often doesn't bind.
- There is **no native solar-priority / battery-before-grid mode.** With grid
  connected the unit keeps grid primary and the battery in reserve. The only
  community-proven workaround is **islanding via a smart plug on the grid input.**

---

## Transport reliability (the big one)

The unit is reachable three ways, and they are **not** equivalent:

| Transport | Reads | Writes |
|-----------|-------|--------|
| **Cloud REST** (`iot-api.landecia.com`) | ✅ full settings + telemetry | ✅ **reliable** (multi-minute lag) |
| Cloud MQTT (`iot-south.landecia.com`) | ✅ telemetry only (not settings) | ⚠️ unreliable |
| Local TCP (`<ip>:6607`) | ❌ returns *no data fields* | ❌ **silently fails** |

Consequences:
- **Live telemetry depends on the internet** (cloud MQTT). The local TCP link
  handshakes and accepts control frames but returns an empty read body
  (`No data fields in local read response`) on this firmware. Setting
  `high_frequency_reporting` to a LAN mode does **not** fix this — tested, local
  reads still come back empty.
- **Any control script must write over cloud REST and verify by reading back over
  cloud REST.** A write that "returns 200" over local TCP has very likely done
  nothing.

REST read:  `GET /v2/binding/enduserapi/getDeviceBusinessAttributes?pk={pk}&dk={dk}`
REST write: `POST /v2/binding/enduserapi/batchControlDevice`  body `{"data": "[{\"code\": value}]", "deviceList": [{"productKey": pk, "deviceKey": dk}], "type": 2}`

With `pecron-monitor`:
```bash
pecron-monitor --raw    --rest-only                       # read everything
pecron-monitor --control ac_charge_stop_value_iaos 50 --rest-only   # write, then re-read to verify
```

---

## Charging model (what actually controls grid charging)

Two separate controls exist and people conflate them:

| Code | Name | What it does |
|------|------|--------------|
| `ac_charge_stop_value_iaos` | **AC Charge Limit** (SOC %) | Grid charges the battery **up to this SOC**, then stops and passes through. **This is the real lever.** Firmware minimum **50%**. |
| `ac_charging_power_ios` | AC Charge **Speed** (enum 0–10 = 0–100%) | Caps charge *power*. Often does **not** bind — natural charge draw can sit below the cap, so raising/lowering it changes nothing unless you set it *below* the natural draw. |

Key behaviours:
- **Solar (DC/MPPT) charges independently of the AC charge limit.** Set the AC
  charge limit to 50% and solar will still fill 50→100%. That's the basis of a
  "grid tops up to X%, solar does the rest" strategy.
- **The AC charge limit can't be set below the *current* SOC in some states**, and
  even when accepted it takes minutes to reflect. Assert it when SOC is low.
- **The unit resets AC charge speed to 100% whenever AC input is cut** (i.e. every
  time you island). Re-assert speed after each grid reconnect if you rely on it.
- Lowering charge speed is the lever to protect a **smart plug on the grid input**
  from over-current: at full speed grid charge draw + load can exceed a 15 A plug's
  continuous rating.

---

## No native solar-priority → the islanding pattern

The F5000LFP's UPS modes (Online 0 ms / Standby 8–20 ms) differ only in *transfer
speed*; **both keep grid primary and battery in reserve.** Flipping UPS mode does
**not** give battery-before-grid. There is no self-consumption mode.

The community workaround (used by Bluetti/EcoFlow/Pecron HA setups): **put a smart
plug on the grid input and toggle it by battery SOC** —
- **island** (plug off) when SOC is high and solar ≥ load → run on battery + solar,
- **force grid on** (plug on) at a low-SOC floor.

**Critical safety note:** the Pecron will **not** reconnect grid on its own and
**will discharge to its cutoff** (≈5%). So your islanding script *is* the only
failsafe — it must reliably restore grid at the floor (back it with a downstream
UPS for a clean shutdown), retry, and alert on failure.

A robust hybrid: enforce the **AC charge limit (REST)** as the everyday cap *and*
keep the **plug** as a physical backstop that islands if grid is ever seen
charging the battery past the cap (i.e. the limit silently reverted).

---

## Useful property codes (TSL)

From `pecron-monitor --controls` (RW = writable, RO = read-only):

| id | code | RW | meaning / notes |
|----|------|----|-----|
| 42 | `ac_charge_stop_value_iaos` | RW | **AC charge limit** (SOC %), min 50 — the grid-charge lever |
| 51 | `ac_charging_power_ios` | RW | AC charge speed enum 0–10 (0–100%); often doesn't bind |
| 53 | `upper_discharge_limit_us` | RW | overall charge ceiling (keep 100 so solar can fill) |
| 54 | `lower_discharge_limit_us` | RW | discharge floor (hardware cutoff, e.g. 5%) |
| 37 | `device_status_hm` | RO | 0=Shutdown 1=Charging 2=DC Discharge 3=AC Discharge 4=Standby 5=Conservation |
| 40 | `eco_quite_mode_as` | RW | ECO auto-shutoff on low load — keep **off** for always-on loads |
| 58 | `ac_nor_open_lab` | RW | **AC Always On** — set **true** so the AC output can't auto-cut |
| 57 | `device_standy_times_as` | RW | standby timeout; `4` = Always On |
| 56 / 55 | `ac_switch_hm` / `dc_switch_hm` | RW | AC / DC output switches |
| 64 / 63 | `total_energy` / `solar_panel_power_generation` | RO | PV generated, lifetime / session (kWh) |
| 68 / 65 | `total_ac_charge_energy` / `ac_charge_energy` | RO | grid charge, lifetime / session (kWh) |
| 72 / 70 | `total_ac_output_energy` / `total_dc_output_energy` | RO | output energy totals (kWh) |
| 73–84 | `day/month/year_{energy,acc,dcd,acd}_data` | RO | per-period history arrays (PV gen, AC charge, DC/AC discharge) |
| 100 | `high_frequency_reporting` | RW | report cadence; does **not** enable local telemetry reads |

The lifetime energy counters (ids 63–72) are the honest source for **solar ROI**
(actual PV generated vs actual grid charged) — far better than integrating power
in your home-automation platform "since you started tracking."

---

## Other gotchas

- `ups_mode` frequently decodes as `unknown` in Home Assistant even though the REST
  `--status` shows the real value. Don't trust the HA decode for it.
- `ac_output_voltage` / `ac_output_frequency` telemetry report nonsense values
  (e.g. 250–310 V, 247–252 Hz). Ignore them.
- Settings writes over cloud REST are accepted (`code: 200`) before they apply —
  always **verify by reading the value back** (after the propagation delay).
- There is a cloud request quota (~hundreds–low thousands/day); don't poll harder
  than you need to.
