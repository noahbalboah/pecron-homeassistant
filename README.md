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

> **Controlling a Pecron yourself? Read [docs/pecron-api-quirks.md](docs/pecron-api-quirks.md) first.**
> It documents the non-obvious control model this tooling is built around — cloud
> REST vs (silently-failing) local-TCP writes, the charge-*limit*-not-*speed* lever,
> no native solar-priority, and the multi-minute write propagation lag.

## Components

| Path | What |
|------|------|
| `mcp/` | A thin **MCP server** — read/control Home Assistant *and* read/set the Pecron's charge settings (charge limit + speed) over the reliable cloud-REST path |
| `scripts/pecron-island.py` | **Solar self-consumption islander** — toggles the grid-input smart plug by SOC (island ≥40% & solar≥load, force grid on ≤25% floor), caps grid charging at 50% via REST so solar fills the rest, with a plug safety-net, charge-speed throttle, and offline alert; verifies + retries |
| `scripts/storm-charge.py` | **Pre-storm grid fast-charge** — on an NWS Severe/Extreme alert, force grid on + AC charge limit 100% to fill the battery before the outage; reverts to 50% (REST, self-verifying) and hands control back to the islander when clear |
| `scripts/pecron-energy-rest.py` | Publishes the Pecron's **native lifetime/session energy counters** (PV generated vs grid charged) into HA for accurate solar ROI |
| `scripts/pecron-log.py` | Read-only **CSV logger** of SOC / solar / load / grid draw for sizing the setup |

## How the islander works

```
SOC ≤ 25%                 → grid ON  (failsafe; the script is the ONLY thing that
                                       reconnects grid — Pecron won't on its own)
SOC ≥ 40% and solar ≥ load → island  (grid OFF, run on battery + solar)
otherwise                 → hold
```

Grid charging is capped with the **AC charge limit** (set to 50% over the cloud
REST API): grid tops the battery to 50% then passes through, and **solar fills
50→100%** independently. The charge *limit* — not the charge *speed* — is the lever
that actually works on this firmware, and it must be written over **cloud REST**
(local-TCP writes silently fail). The islander asserts it at the floor and keeps the
smart plug as a physical backstop that islands if grid is ever seen charging past
the cap. See **[docs/pecron-api-quirks.md](docs/pecron-api-quirks.md)** for the full
control model and the traps behind all of this.

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
