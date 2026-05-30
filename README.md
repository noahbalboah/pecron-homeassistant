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
| `scripts/pecron-island.py` | **Solar self-consumption islander** — toggles the grid-input smart plug by SOC (island ≥40% & solar≥load, force grid on ≤25% floor), enforces 0% grid-charge so only solar charges the battery, verifies + retries + iPhone alert on failure |
| `scripts/storm-charge.py` | **Pre-storm grid fast-charge** — on an NWS Severe/Extreme alert, force grid on + 80% charge speed to fill the battery before the outage; hands control back to the islander when clear |
| `scripts/pecron-log.py` | Read-only **CSV logger** of SOC / solar / load / grid draw for sizing the setup |

## How the islander works

```
SOC ≤ 25%                 → grid ON  (failsafe; the script is the ONLY thing that
                                       reconnects grid — Pecron won't on its own)
SOC ≥ 40% and solar ≥ load → island  (grid OFF, run on battery + solar)
otherwise                 → hold
```

Baseline assumes **AC charging speed 0%** (grid passes through to cover the load
but never charges the battery) and **AC charge limit 100%** (solar can fill it).
The Pecron resets charging speed to 100% whenever AC input is cut, so the islander
re-forces 0% on every grid-on.

## ⚠️ Safety

Islanding **removes the hardware grid backstop** — nothing reconnects grid except
this script. If it fails to switch the plug back on at the floor, the Pecron
discharges to its low-voltage cutoff and drops everything downstream. **Run a UPS
downstream** for a clean shutdown, keep the SOC floor conservative, and watch the
first cycles. Use at your own risk.

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
