# Pecron F5000LFP × Home Assistant — solar self-consumption automation

Tooling to monitor and control a **Pecron F5000LFP** portable power station from
Home Assistant, and to run **battery-priority solar self-consumption** that the
Pecron firmware doesn't support natively.

## Why this exists

The Pecron F5000LFP (like most portable power stations) has **no native
self-consumption / battery-before-grid mode**. Its UPS modes (Online 0 ms /
Standby 8–20 ms) only differ in *transfer speed* — both keep the **grid as the
primary source and the battery as reserve**. So when grid is connected, it powers
your loads from the grid and never discharges the stored solar.

The documented community workaround for these units is to **toggle a smart plug
on the grid input by battery SOC**: cut grid to force the unit onto solar +
battery (islanding), and reconnect grid at a low-SOC floor. That's what this does.

## Components

| Path | What |
|------|------|
| `mcp/` | A thin **Home Assistant MCP server** (REST wrapper) — read any entity, fetch history, call any service, plus a `pecron_status()` bundle |
| `scripts/pecron-island.py` | **Solar self-consumption islander** — toggles the grid-input smart plug by SOC (island ≥40% & solar≥load, force grid on ≤25% floor), asserts a 50% AC charge limit at the floor, islands if grid keeps charging past 55% (limit silently reverted), verifies + retries + iPhone alert on failure. **Read the plug warning under Safety first.** |
| `scripts/storm-charge.py` | **Pre-storm grid fast-charge** — on an outage-risk NWS alert (tornado, thunderstorm, wind, winter storm…; flood/heat/fog ignored), force grid on and raise the AC charge limit to 100% to fill before the outage; on clear, set 50% back and keep re-asserting until a read-back confirms it |
| `scripts/pecron-charge-limit.py` | **AC charge-limit guard** — keeps the AC charge limit at 50% via pecron-monitor's cloud REST helpers (the firmware can silently revert it to 100%). Waits 60 min between writes for the cloud's propagation lag; yields during a storm pre-charge. Works without a smart plug |
| `scripts/pecron-log.py` | Read-only **CSV logger** of SOC / solar / load / grid draw for sizing the setup |

## How the islander works

```
SOC ≤ 25%                 → grid ON  (failsafe; the script is the ONLY thing that
                                       reconnects grid — Pecron won't on its own)
SOC ≥ 40% and solar ≥ load → island  (grid OFF, run on battery + solar)
otherwise                 → hold
```

The lever that actually governs grid charging on the F5000LFP is the **AC charge
limit** (`ac_charge_stop_value_iaos`, minimum 50%), not the charge speed (which
often doesn't bind). With the limit at 50%, grid tops the battery to 50% and then
only passes through; solar fills 50→100%. The firmware rejects a limit below the
current SOC, so the islander asserts it at the floor, and cloud writes take
minutes to show in a read-back.

## ⚠️ Safety

Islanding **removes the hardware grid backstop** — nothing reconnects grid except
this script. If it fails to switch the plug back on at the floor, the Pecron
discharges to its low-voltage cutoff and drops everything downstream. **Run a UPS
downstream** for a clean shutdown, keep the SOC floor conservative, and watch the
first cycles. Use at your own risk.

**Size the grid-input switch for the real load. A 15 A smart plug burned out in
this setup.** A TP-Link EP25 (15 A, 1,800 W max) on the Pecron's grid input
carried the homelab load *plus* battery charging. Logs show months of nightly
draws at or above 1,440 W (80% of its rating, the limit for continuous loads),
peaking at 1,856 W, before it failed. Capping the charge speed at 60% was not
enough, because the load passes through on top of the charging current. Use a
device rated well above load + max charge power (for example a 20 A relay on a
20 A circuit), or lower the charge speed until the total stays under 80% of the
switch's rating. The author has since retired the islander and runs on
`pecron-charge-limit.py` alone, with grid wired directly.

## Requirements / adapting it

- A **Pecron** power station surfaced in Home Assistant via
  [attractify-logan/pecron-monitor](https://github.com/attractify-logan/pecron-monitor)
- A **TP-Link / Kasa smart plug with energy monitoring** on the Pecron's grid (AC) input
- **Home Assistant** + a long-lived token (provided via env / Doppler — never hardcoded)
- An **NWS alert sensor** for `storm-charge.py` (optional)

Replace the environment-specific bits with your own: the device-key in the
`sensor.pecron_f5000lfp_<key>_*` entity IDs, the smart-plug entity
(`switch.network_server_rack`), the container name, and the `/home/noah/...`
paths. Scripts read `HA_TOKEN` from the environment.

## License

MIT
