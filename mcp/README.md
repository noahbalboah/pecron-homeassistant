# ha-mcp

A thin MCP server that lets Claude talk to Home Assistant over its REST API —
read any entity, fetch history, and call any service. Built primarily to monitor
and control the Pecron F5000LFP power station, but scoped to the whole home.

## Tools

| Tool | Purpose |
|------|---------|
| `ha_find(query, domain?)` | Search entities by substring (id or friendly name), optional domain filter |
| `ha_get(entity_ids)` | Full state + attributes for specific entities |
| `ha_history(entity_id, hours, max_points?)` | Recent history, downsampled |
| `ha_call_service(domain, service, data?, entity_id?)` | Call any HA service (control path) |
| `pecron_status()` | Convenience bundle of key Pecron values (from HA) |
| `pecron_get_settings()` | Read the Pecron's charge limit/speed/discharge limits + energy counters over **cloud REST** (the path HA/MQTT can't see) |
| `pecron_set_charge_limit(percent)` | Set the AC charge limit (SOC %, min 50) over cloud REST — the reliable charge lever |
| `pecron_set_charge_speed(percent)` | Set the AC charge speed (0–100%) over cloud REST |

The three `pecron_*` settings/control tools shell out to
[`pecron-monitor`](https://github.com/attractify-logan/pecron-monitor) over its
cloud-REST path, because local-TCP/MQTT writes silently fail and don't expose
settings (see [`../docs/pecron-api-quirks.md`](../docs/pecron-api-quirks.md)).
They're disabled unless `PECRON_MONITOR_CMD` is set (below).

## Secrets (Doppler)

Config lives in Doppler project `home-network`, config `dev`:

- `HA_BASE_URL` — `http://homeassistant.local:8123`
- `HA_TOKEN` — a Home Assistant long-lived access token
- `PECRON_MONITOR_CMD` *(optional)* — command prefix to invoke `pecron-monitor` for the
  `pecron_*` control tools, e.g. `docker exec pecron-monitor-pecron-monitor-1 pecron-monitor --config /config/config.yaml` (unset = those tools disabled)

Set the token (never committed, never echoed):

```bash
# 1. In HA: Profile → Security → Long-lived access tokens → Create ("claude-mcp")
# 2. Paste it into Doppler (input hidden):
doppler secrets set HA_TOKEN -p home-network -c dev
```

## Run / register

Registered for Claude Code in `../.mcp.json`, launched via:

```bash
doppler run -p home-network -c dev -- ./.venv/bin/python server.py
```

The token is injected at runtime by Doppler — it never lands on disk.
Restart Claude Code after setting the token for the `home-assistant` MCP to load.

## Setup from scratch

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```
