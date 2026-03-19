#!/usr/bin/env python3
"""
HA-Matrix-Alerts — Home Assistant Weather/EAS Alert Bridge

Monitors Home Assistant for weather alerts (NWS, EAS) and forwards them
to Matrix rooms. When combined with a Meshtastic bridge, alerts propagate
to mesh radios automatically.

Alert flow:
  Home Assistant → this bot → Matrix rooms → (optional) Meshtastic bridge → mesh radios

Supports:
  - NWS Weather Alerts (via HA NWS integration)
  - EAS/Emergency Alert System (via HA alert entities)
  - Custom HA sensor alerts (configurable thresholds)
  - Severe weather watches, warnings, and advisories
  - Alert deduplication (won't spam the same alert twice)
  - Configurable severity filtering
  - Rate limiting to prevent flooding

Configuration: Copy config.example.yaml to config.yaml and edit.

Dependencies:
  - Home Assistant with NWS integration configured
  - Matrix account for the bot
  - Optional: meshtastic-matrix-bridge for mesh delivery

Related projects:
  - meshtastic-matrix-bridge: https://github.com/dakotasnapshot/meshtastic-matrix-bridge
  - meshbot:                  https://github.com/dakotasnapshot/meshbot
  - matrixbot:                https://github.com/dakotasnapshot/matrixbot
"""

import asyncio
import hashlib
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp
import yaml
from nio import AsyncClient

# ---------------------------
# Configuration
# ---------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}

    return {
        "homeassistant": {
            "url": os.environ.get("HA_URL", "http://homeassistant.local:8123"),
            "token": os.environ.get("HA_TOKEN", ""),
            "verify_ssl": os.environ.get("HA_VERIFY_SSL", "true").lower() == "true",
        },
        "matrix": {
            "homeserver": os.environ.get("MATRIX_HOMESERVER", "https://matrix.example.com"),
            "bot_user_id": os.environ.get("MATRIX_BOT_USER_ID", "@alerts:matrix.example.com"),
            "access_token": os.environ.get("MATRIX_ACCESS_TOKEN", ""),
        },
        "rooms": os.environ.get("ALERT_ROOMS", "").split(","),
        "alerts": {
            "nws_entity": os.environ.get("NWS_ENTITY", "sensor.nws_alerts"),
            "poll_interval": int(os.environ.get("POLL_INTERVAL", "60")),
            "min_severity": os.environ.get("MIN_SEVERITY", "moderate"),
            "include_watches": os.environ.get("INCLUDE_WATCHES", "true").lower() == "true",
            "include_advisories": os.environ.get("INCLUDE_ADVISORIES", "false").lower() == "true",
            "dedup_hours": int(os.environ.get("DEDUP_HOURS", "24")),
            "rate_limit_seconds": int(os.environ.get("RATE_LIMIT", "30")),
            "mesh_friendly": os.environ.get("MESH_FRIENDLY", "true").lower() == "true",
        },
        "custom_sensors": [],
    }


cfg = load_config()

# Home Assistant
HA_URL = cfg["homeassistant"]["url"].rstrip("/")
HA_TOKEN = cfg["homeassistant"]["token"]
HA_VERIFY_SSL = cfg["homeassistant"].get("verify_ssl", True)

# Matrix
MATRIX_HS = cfg["matrix"]["homeserver"]
BOT_USER_ID = cfg["matrix"]["bot_user_id"]
MATRIX_TOKEN = cfg["matrix"]["access_token"]
ALERT_ROOMS = [r.strip() for r in cfg.get("rooms", []) if r.strip()]

# Alert settings
NWS_ENTITY = cfg.get("alerts", {}).get("nws_entity", "sensor.nws_alerts")
POLL_INTERVAL = cfg.get("alerts", {}).get("poll_interval", 60)
MIN_SEVERITY = cfg.get("alerts", {}).get("min_severity", "moderate")
INCLUDE_WATCHES = cfg.get("alerts", {}).get("include_watches", True)
INCLUDE_ADVISORIES = cfg.get("alerts", {}).get("include_advisories", False)
DEDUP_HOURS = cfg.get("alerts", {}).get("dedup_hours", 24)
RATE_LIMIT_SECONDS = cfg.get("alerts", {}).get("rate_limit_seconds", 30)
MESH_FRIENDLY = cfg.get("alerts", {}).get("mesh_friendly", True)

# Custom sensor monitoring
CUSTOM_SENSORS = cfg.get("custom_sensors", [])

# Severity levels (ordered)
SEVERITY_ORDER = ["minor", "moderate", "severe", "extreme"]
SEVERITY_EMOJI = {
    "minor": "🟡",
    "moderate": "🟠",
    "severe": "🔴",
    "extreme": "🚨",
    "unknown": "⚠️",
}

# ---------------------------
# Alert Deduplication
# ---------------------------

class AlertTracker:
    """Track sent alerts to prevent duplicates."""

    def __init__(self, state_file: str = "alert_state.json"):
        self.state_file = Path(__file__).parent / state_file
        self.sent: Dict[str, float] = {}
        self.last_send_time: float = 0
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                self.sent = data.get("sent", {})
            except Exception:
                self.sent = {}

    def _save(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"sent": self.sent}, f, indent=2)
        except Exception as e:
            print(f"[WARN] Could not save state: {e}")

    def alert_hash(self, alert: dict) -> str:
        """Generate a unique hash for an alert."""
        event = alert.get("event", "")
        headline = alert.get("headline", "")
        onset = alert.get("onset", "")
        key = f"{event}|{headline}|{onset}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    def is_new(self, alert: dict) -> bool:
        """Check if alert hasn't been sent recently."""
        h = self.alert_hash(alert)
        sent_time = self.sent.get(h)
        if sent_time is None:
            return True
        age_hours = (time.time() - sent_time) / 3600
        return age_hours > DEDUP_HOURS

    def mark_sent(self, alert: dict):
        """Mark alert as sent."""
        h = self.alert_hash(alert)
        self.sent[h] = time.time()
        self._prune()
        self._save()

    def _prune(self):
        """Remove old entries."""
        cutoff = time.time() - (DEDUP_HOURS * 3600 * 2)
        self.sent = {k: v for k, v in self.sent.items() if v > cutoff}

    def rate_ok(self) -> bool:
        """Check if we're within rate limits."""
        if time.time() - self.last_send_time < RATE_LIMIT_SECONDS:
            return False
        return True

    def mark_rate(self):
        self.last_send_time = time.time()


tracker = AlertTracker()

# ---------------------------
# Home Assistant API
# ---------------------------

async def ha_get_state(session: aiohttp.ClientSession, entity_id: str) -> Optional[dict]:
    """Get entity state from Home Assistant."""
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    ssl_context = None
    if not HA_VERIFY_SSL:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with session.get(url, headers=headers, ssl=ssl_context) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                print(f"[ERROR] HA API returned {resp.status} for {entity_id}")
                return None
    except Exception as e:
        print(f"[ERROR] HA API error for {entity_id}: {e}")
        return None


def parse_nws_alerts(state: dict) -> List[dict]:
    """Parse NWS alerts from HA entity state."""
    alerts = []
    attrs = state.get("attributes", {})

    # Handle different NWS integration formats
    # Format 1: alerts stored in attributes directly
    raw_alerts = attrs.get("alerts", [])
    if isinstance(raw_alerts, list):
        alerts.extend(raw_alerts)

    # Format 2: individual alert attributes (alert_1, alert_2, etc.)
    for key in sorted(attrs.keys()):
        if key.startswith("alert_") and isinstance(attrs[key], dict):
            alerts.append(attrs[key])

    # Format 3: event_type with details in attributes
    if not alerts and attrs.get("event"):
        alerts.append({
            "event": attrs.get("event", ""),
            "headline": attrs.get("headline", attrs.get("friendly_name", "")),
            "severity": attrs.get("severity", "unknown"),
            "description": attrs.get("description", ""),
            "instruction": attrs.get("instruction", ""),
            "onset": attrs.get("onset", ""),
            "expires": attrs.get("expires", ""),
            "certainty": attrs.get("certainty", ""),
            "urgency": attrs.get("urgency", ""),
        })

    return alerts


def should_send(alert: dict) -> bool:
    """Determine if alert meets severity/type criteria."""
    severity = alert.get("severity", "unknown").lower()
    event = alert.get("event", "").lower()

    # Check severity threshold
    if severity in SEVERITY_ORDER:
        if SEVERITY_ORDER.index(severity) < SEVERITY_ORDER.index(MIN_SEVERITY):
            return False

    # Filter watches and advisories if configured
    if not INCLUDE_WATCHES and "watch" in event:
        return False
    if not INCLUDE_ADVISORIES and "advisory" in event:
        return False

    return True


def format_alert(alert: dict) -> str:
    """Format alert for Matrix/mesh delivery."""
    severity = alert.get("severity", "unknown").lower()
    emoji = SEVERITY_EMOJI.get(severity, "⚠️")
    event = alert.get("event", "Unknown Alert")
    headline = alert.get("headline", "")

    if MESH_FRIENDLY:
        # Short format for mesh delivery (under 200 chars)
        msg = f"{emoji} {event}"
        if headline and headline != event:
            remaining = 195 - len(msg)
            if remaining > 20:
                msg += f": {headline[:remaining]}"
        expires = alert.get("expires", "")
        if expires:
            try:
                exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                msg += f" (until {exp.strftime('%I:%M%p %Z')})"
            except Exception:
                pass
        return msg[:200]
    else:
        # Full format for Matrix
        lines = [f"{emoji} **{event}**"]
        if headline:
            lines.append(headline)
        description = alert.get("description", "")
        if description:
            lines.append("")
            lines.append(description[:500])
        instruction = alert.get("instruction", "")
        if instruction:
            lines.append("")
            lines.append(f"**Action:** {instruction[:300]}")
        expires = alert.get("expires", "")
        if expires:
            try:
                exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                lines.append(f"Expires: {exp.strftime('%B %d at %I:%M %p %Z')}")
            except Exception:
                pass
        return "\n".join(lines)


def format_sensor_alert(sensor_name: str, value: str, threshold: str, condition: str) -> str:
    """Format a custom sensor alert."""
    if MESH_FRIENDLY:
        return f"⚠️ {sensor_name}: {value} ({condition} {threshold})"
    else:
        return f"⚠️ **Sensor Alert: {sensor_name}**\nCurrent value: {value}\nThreshold: {condition} {threshold}"


# ---------------------------
# Matrix
# ---------------------------

matrix_client = AsyncClient(MATRIX_HS)
matrix_client.user_id = BOT_USER_ID
matrix_client.access_token = MATRIX_TOKEN


async def send_to_matrix(message: str):
    """Send alert to all configured Matrix rooms."""
    for room_id in ALERT_ROOMS:
        try:
            await matrix_client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": message},
            )
            print(f"[TX] → {room_id}")
        except Exception as e:
            print(f"[ERROR] Failed to send to {room_id}: {e}")


# ---------------------------
# Custom Sensor Monitoring
# ---------------------------

async def check_custom_sensors(session: aiohttp.ClientSession):
    """Check custom HA sensors against configured thresholds."""
    for sensor_cfg in CUSTOM_SENSORS:
        entity_id = sensor_cfg.get("entity_id")
        threshold = sensor_cfg.get("threshold")
        condition = sensor_cfg.get("condition", "above")
        name = sensor_cfg.get("name", entity_id)

        if not entity_id or threshold is None:
            continue

        state = await ha_get_state(session, entity_id)
        if not state:
            continue

        try:
            value = float(state.get("state", 0))
            threshold_val = float(threshold)
        except (ValueError, TypeError):
            continue

        triggered = False
        if condition == "above" and value > threshold_val:
            triggered = True
        elif condition == "below" and value < threshold_val:
            triggered = True
        elif condition == "equals" and value == threshold_val:
            triggered = True

        if triggered:
            alert_data = {"event": f"sensor_{entity_id}", "headline": f"{name}={value}"}
            if tracker.is_new(alert_data) and tracker.rate_ok():
                msg = format_sensor_alert(name, str(value), str(threshold), condition)
                await send_to_matrix(msg)
                tracker.mark_sent(alert_data)
                tracker.mark_rate()
                print(f"[ALERT] Custom sensor: {name} = {value} ({condition} {threshold})")


# ---------------------------
# Main Loop
# ---------------------------

async def poll_loop():
    """Main polling loop — checks HA for alerts."""
    ssl_context = None
    if not HA_VERIFY_SSL:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_context) if ssl_context else None
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                # Check NWS alerts
                state = await ha_get_state(session, NWS_ENTITY)
                if state:
                    alerts = parse_nws_alerts(state)
                    for alert in alerts:
                        if should_send(alert) and tracker.is_new(alert):
                            if tracker.rate_ok():
                                msg = format_alert(alert)
                                await send_to_matrix(msg)
                                tracker.mark_sent(alert)
                                tracker.mark_rate()
                                print(f"[ALERT] {alert.get('event', 'Unknown')}: {alert.get('headline', '')[:80]}")
                            else:
                                print(f"[RATE] Skipping alert (rate limited): {alert.get('event', '')}")

                # Check custom sensors
                if CUSTOM_SENSORS:
                    await check_custom_sensors(session)

            except Exception as e:
                print(f"[ERROR] Poll error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


async def main():
    print(f"🚨 HA-Matrix-Alerts starting")
    print(f"   HA: {HA_URL}")
    print(f"   Matrix: {MATRIX_HS} as {BOT_USER_ID}")
    print(f"   NWS entity: {NWS_ENTITY}")
    print(f"   Min severity: {MIN_SEVERITY}")
    print(f"   Rooms: {len(ALERT_ROOMS)}")
    print(f"   Poll interval: {POLL_INTERVAL}s")
    print(f"   Mesh-friendly: {MESH_FRIENDLY}")
    print(f"   Custom sensors: {len(CUSTOM_SENSORS)}")

    # Sync Matrix client
    await matrix_client.sync(timeout=10000, full_state=True)
    print("✅ Matrix connected")

    # Start polling
    print("✅ Alert monitoring LIVE")
    await poll_loop()


if __name__ == "__main__":
    asyncio.run(main())
