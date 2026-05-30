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
| `pecron_status()` | Convenience bundle of key Pecron values |

## Secrets (Doppler)

Config lives in Doppler project `home-network`, config `dev`:

- `HA_BASE_URL` — `http://homeassistant.local:8123`
- `HA_TOKEN` — a Home Assistant long-lived access token

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
