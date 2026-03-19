# 🚨 HA-Matrix-Alerts — Home Assistant Weather/EAS Bridge

Monitors [Home Assistant](https://www.home-assistant.io/) for weather alerts and emergency notifications (NWS, EAS), then forwards them to [Matrix](https://matrix.org/) rooms. When paired with a [Meshtastic bridge](https://github.com/dakotasnapshot/meshtastic-matrix-bridge), alerts propagate to mesh radios automatically — **full EAS over Meshtastic**.

No API keys needed (uses your existing HA setup).

## Alert Flow

```
┌─────────────────┐      ┌──────────────────┐      ┌──────────┐      ┌────────────┐
│  NWS / NOAA     │─────►│  Home Assistant   │─────►│  This    │─────►│   Matrix   │
│  Weather Alerts │      │  (NWS integration)│      │  Bot     │      │   Room     │
│  EAS Alerts     │      │                   │      │          │      │            │
│  Custom Sensors │      │  Sensors/Entities │      │          │      │            │
└─────────────────┘      └──────────────────┘      └──────────┘      └──────┬─────┘
                                                                           │
                                                                    ┌──────▼─────┐
                                                                    │ Meshtastic │
                                                                    │ Bridge     │
                                                                    │ (optional) │
                                                                    └──────┬─────┘
                                                                           │
                                                                    ┌──────▼─────┐
                                                                    │   Mesh     │
                                                                    │  Radios 📡 │
                                                                    └────────────┘
```

## Features

- **NWS Weather Alerts** — Tornado warnings, severe thunderstorm, winter storms, flood warnings, etc.
- **EAS Alerts** — Emergency Alert System notifications via Home Assistant
- **Custom Sensor Monitoring** — Alert on any HA sensor crossing a threshold (temperature, wind, battery, etc.)
- **Severity Filtering** — Only forward alerts above your configured level (minor → moderate → severe → extreme)
- **Alert Deduplication** — Won't spam the same alert repeatedly (configurable window)
- **Rate Limiting** — Prevents flooding during alert storms
- **Mesh-Friendly Format** — Short messages (under 200 chars) optimized for Meshtastic delivery
- **Full Format Option** — Rich Matrix messages with full descriptions when mesh isn't needed
- **Persistent State** — Remembers sent alerts across restarts

## Quick Start

### Prerequisites

- Python 3.10+
- Home Assistant with [NWS integration](https://www.home-assistant.io/integrations/nws/) configured
- A Matrix account for the bot
- A [Long-Lived Access Token](https://www.home-assistant.io/docs/authentication/#your-account-profile) from Home Assistant

### Dependencies

| Package | Purpose | Install |
|---------|---------|---------|
| `matrix-nio` | Matrix client library | `pip install matrix-nio` |
| `aiohttp` | Async HTTP for HA API | `pip install aiohttp` |
| `PyYAML` | Config file parsing | `pip install pyyaml` |

All installed automatically via `requirements.txt`.

### Installation

```bash
git clone https://github.com/dakotasnapshot/ha-matrix-alerts.git
cd ha-matrix-alerts

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# Edit config.yaml with your HA and Matrix credentials
```

### Configuration

```yaml
homeassistant:
  url: "https://homeassistant.local:8123"
  token: "your_long_lived_access_token"
  verify_ssl: false

matrix:
  homeserver: "https://matrix.example.com"
  bot_user_id: "@alerts:matrix.example.com"
  access_token: "syt_your_token"

rooms:
  - "!alert_room:matrix.example.com"
  - "!meshtastic_room:matrix.example.com"   # Bridge room → mesh delivery

alerts:
  nws_entity: "sensor.nws_alerts"
  poll_interval: 60
  min_severity: "moderate"
  include_watches: true
  mesh_friendly: true          # Short format for mesh delivery
```

### Getting a Home Assistant Token

1. Go to your HA instance → **Profile** (bottom-left)
2. Scroll to **Long-Lived Access Tokens**
3. Click **Create Token**
4. Name it "matrix-alerts" and copy the token

### Getting a Matrix Access Token

```bash
curl -s -X POST "https://matrix.example.com/_matrix/client/r0/login" \
  -H "Content-Type: application/json" \
  -d '{"type":"m.login.password","user":"alerts","password":"your_password"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
```

### Finding Your NWS Entity

In Home Assistant:
1. Go to **Developer Tools** → **States**
2. Search for `nws` or `weather`
3. Common entities: `sensor.nws_alerts`, `sensor.weatheralerts`
4. Your entity might include your county name

### Run

```bash
# Direct
python ha_matrix_alerts.py

# As a systemd service (recommended)
sudo cp systemd/ha-matrix-alerts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ha-matrix-alerts

# Check logs
journalctl -u ha-matrix-alerts -f
```

## Custom Sensor Alerts

Monitor any HA sensor and alert when thresholds are crossed:

```yaml
custom_sensors:
  - entity_id: "sensor.outdoor_temperature"
    name: "Outdoor Temp"
    threshold: 100
    condition: "above"

  - entity_id: "sensor.wind_speed"
    name: "Wind Speed"
    threshold: 50
    condition: "above"

  - entity_id: "sensor.battery_soc"
    name: "Battery SOC"
    threshold: 10
    condition: "below"
```

Conditions: `above`, `below`, `equals`

## Meshtastic Integration

To get alerts on mesh radios:

1. Set up [meshtastic-matrix-bridge](https://github.com/dakotasnapshot/meshtastic-matrix-bridge) in a Matrix room
2. Add that room to this bot's `rooms` list
3. Set `mesh_friendly: true` for short-format alerts
4. Alerts flow: HA → this bot → Matrix room → bridge → mesh radios

That's it. No extra configuration on the mesh side.

## Severity Levels

| Level | Emoji | Examples |
|-------|-------|----------|
| Minor | 🟡 | Frost advisory, beach hazard |
| Moderate | 🟠 | Wind advisory, winter weather advisory |
| Severe | 🔴 | Severe thunderstorm warning, winter storm warning |
| Extreme | 🚨 | Tornado warning, flash flood emergency |

## Environment Variables (Alternative to config.yaml)

| Variable | Description |
|----------|-------------|
| `HA_URL` | Home Assistant URL |
| `HA_TOKEN` | Long-lived access token |
| `HA_VERIFY_SSL` | Verify SSL cert (true/false) |
| `MATRIX_HOMESERVER` | Matrix server URL |
| `MATRIX_BOT_USER_ID` | Bot's Matrix user ID |
| `MATRIX_ACCESS_TOKEN` | Bot's Matrix access token |
| `ALERT_ROOMS` | Comma-separated room IDs |
| `NWS_ENTITY` | HA entity for NWS alerts |
| `POLL_INTERVAL` | Check interval in seconds |
| `MIN_SEVERITY` | Minimum severity level |
| `MESH_FRIENDLY` | Short format (true/false) |

## Related Projects

| Repo | Purpose |
|------|---------|
| **[meshtastic-matrix-bridge](https://github.com/dakotasnapshot/meshtastic-matrix-bridge)** | Meshtastic ⇄ Matrix relay |
| **[meshbot](https://github.com/dakotasnapshot/meshbot)** | Standalone Meshtastic auto-responder |
| **[matrixbot](https://github.com/dakotasnapshot/matrixbot)** | Matrix room auto-responder |
| **ha-matrix-alerts** (this repo) | HA weather/EAS → Matrix → mesh |

## License

MIT — see [LICENSE](LICENSE).

## Credits

- Built with [matrix-nio](https://github.com/poljar/matrix-nio) and [aiohttp](https://github.com/aio-libs/aiohttp)
- Weather data from [NOAA/NWS](https://www.weather.gov/) via Home Assistant
- Made for the [OKMesh](https://okmesh.org) community 📡
