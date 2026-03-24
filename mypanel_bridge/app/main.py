"""
MyPanel Bridge – Home Assistant Add-on
=======================================
Bridges an ESP32 MQTT touch-panel to Home Assistant.

Data flow
---------
ESP32 panel  <──MQTT──>  this bridge  <──WS / REST──>  Home Assistant

MQTT topics (default prefix ``mypanel``):
    mypanel/status            – panel LWT (online / offline)
    mypanel/cmd               – JSON command from panel  →  HA service call
    mypanel/entities          – full entity list  →  panel
    mypanel/state             – individual entity state update  →  panel
    mypanel/discovery/request – panel requests a full entity dump
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt
import websockets

# ---------------------------------------------------------------------------
# Constants / configuration
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("MYPANEL_LOG_LEVEL", "info").upper()
TOPIC_PREFIX = os.environ.get("MYPANEL_TOPIC_PREFIX", "mypanel")
DISCOVERY_INTERVAL = int(os.environ.get("MYPANEL_DISCOVERY_INTERVAL", "300"))

MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core")
HA_WS_URL = HA_BASE_URL.replace("http://", "ws://").replace(
    "https://", "wss://"
) + "/websocket"

# Feature bitmask matching ESP32 panel expectations
FEATURE_BRIGHTNESS = 1
FEATURE_COLOR_TEMP = 2
FEATURE_RGB_COLOR = 4
FEATURE_SPEED = 8

# HA supported_features bits we care about (light domain)
HA_LIGHT_SUPPORT_BRIGHTNESS = 1
HA_LIGHT_SUPPORT_COLOR_TEMP = 2
HA_LIGHT_SUPPORT_COLOR = 16  # HS / XY / RGB grouped under "color"

# Reconnection timing
MQTT_RECONNECT_BASE = 2
MQTT_RECONNECT_MAX = 60
WS_RECONNECT_BASE = 2
WS_RECONNECT_MAX = 60

STATUS_PORT = int(os.environ.get("STATUS_PORT", "8099"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mypanel")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ha_headers() -> dict[str, str]:
    """Return HTTP headers required for Supervisor / HA REST calls."""
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".")[0]


def _map_features(domain: str, ha_features: int, attrs: dict) -> int:
    """Translate HA supported_features into the panel bitmask."""
    flags = 0
    if domain == "light":
        # HA 2024+ uses "supported_color_modes" in addition to the legacy
        # bitmask.  Honour both paths so the bridge works across versions.
        color_modes: list[str] = attrs.get("supported_color_modes", [])
        if ha_features & HA_LIGHT_SUPPORT_BRIGHTNESS or any(
            m in color_modes
            for m in ("brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww")
        ):
            flags |= FEATURE_BRIGHTNESS
        if ha_features & HA_LIGHT_SUPPORT_COLOR_TEMP or "color_temp" in color_modes:
            flags |= FEATURE_COLOR_TEMP
        if ha_features & HA_LIGHT_SUPPORT_COLOR or any(
            m in color_modes for m in ("hs", "xy", "rgb", "rgbw", "rgbww")
        ):
            flags |= FEATURE_RGB_COLOR
    elif domain == "fan":
        # Fans always get SPEED if they report percentage
        if attrs.get("percentage") is not None or ha_features & 1:
            flags |= FEATURE_SPEED
    return flags


def _entity_to_panel_json(entity_id: str, state_obj: dict) -> dict | None:
    """Convert a HA state object into the compact JSON the panel expects.

    Returns ``None`` if the entity should be skipped.
    """
    domain = _entity_domain(entity_id)
    state: str = state_obj.get("state", "unavailable")
    attrs: dict = state_obj.get("attributes", {})

    # Filter: only temperature sensors
    if domain == "sensor":
        if attrs.get("device_class") != "temperature":
            return None

    is_on = state not in ("off", "unavailable", "unknown")

    entry: dict[str, Any] = {
        "id": entity_id,
        "name": attrs.get("friendly_name", entity_id),
        "type": domain,
        "on": is_on,
    }

    if domain == "light":
        entry["brightness"] = attrs.get("brightness", 0) or 0
        entry["color_temp"] = attrs.get("color_temp", 0) or 0
        rgb = attrs.get("rgb_color") or [0, 0, 0]
        entry["r"] = rgb[0]
        entry["g"] = rgb[1]
        entry["b"] = rgb[2]
    elif domain == "fan":
        entry["speed"] = attrs.get("percentage", 0) or 0
    elif domain == "sensor":
        entry["value"] = state
        entry["unit"] = attrs.get("unit_of_measurement", "")

    ha_features = attrs.get("supported_features", 0) or 0
    entry["features"] = _map_features(domain, ha_features, attrs)
    entry["area"] = attrs.get("area_id") or attrs.get("area", "")

    return entry


# ---------------------------------------------------------------------------
# Bridge class
# ---------------------------------------------------------------------------

class MyPanelBridge:
    """Orchestrates the MQTT ↔ HA bridge."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mqtt: mqtt.Client | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._ws_id: int = 0
        self._entities: dict[str, dict] = {}  # entity_id → panel JSON
        self._panel_online: bool = False
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._mqtt_connected: bool = False
        self._areas: dict[str, str] = {}  # area_id → area name

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _setup_mqtt(self) -> mqtt.Client:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="mypanel-bridge",
            protocol=mqtt.MQTTv5,
        )
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_connect = self._on_mqtt_connect
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_message = self._on_mqtt_message
        client.will_set(
            f"{TOPIC_PREFIX}/bridge/status", payload="offline", qos=1, retain=True
        )
        return client

    def _on_mqtt_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        log.info("MQTT connected (rc=%s)", rc)
        self._mqtt_connected = True
        client.subscribe(f"{TOPIC_PREFIX}/cmd", qos=1)
        client.subscribe(f"{TOPIC_PREFIX}/status", qos=1)
        client.subscribe(f"{TOPIC_PREFIX}/discovery/request", qos=0)
        client.publish(
            f"{TOPIC_PREFIX}/bridge/status", payload="online", qos=1, retain=True
        )

    def _on_mqtt_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any = None,
        rc: Any = None,
        properties: Any = None,
    ) -> None:
        self._mqtt_connected = False
        if rc is not None and rc != 0:
            log.warning("MQTT unexpected disconnect (rc=%s) – will reconnect", rc)
        else:
            log.info("MQTT disconnected cleanly")

    def _on_mqtt_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        log.debug("MQTT ← %s: %s", topic, payload[:200])

        if topic == f"{TOPIC_PREFIX}/status":
            self._panel_online = payload == "online"
            log.info("Panel status: %s", payload)
            if self._panel_online and self._loop:
                # Panel just came online – push full entity list
                asyncio.run_coroutine_threadsafe(
                    self._publish_all_entities(), self._loop
                )
        elif topic == f"{TOPIC_PREFIX}/discovery/request":
            log.info("Panel requested discovery")
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._publish_all_entities(), self._loop
                )
        elif topic == f"{TOPIC_PREFIX}/cmd":
            self._handle_panel_command(payload)

    def _handle_panel_command(self, payload: str) -> None:
        """Parse a command from the panel and schedule an HA service call."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.error("Invalid JSON in panel command: %s", payload[:200])
            return

        entity_id = data.get("id", "")
        service = data.get("service", "")
        if not entity_id or not service:
            log.warning("Panel command missing id or service: %s", data)
            return

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._call_ha_service(entity_id, service, data), self._loop
            )

    def _mqtt_publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._mqtt and self._mqtt_connected:
            self._mqtt.publish(topic, payload, qos=1, retain=retain)

    # ------------------------------------------------------------------
    # HA REST API helpers
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=_ha_headers())
        return self._session

    async def _ha_get(self, path: str) -> Any:
        session = await self._ensure_session()
        url = f"{HA_BASE_URL}{path}"
        log.debug("HA GET %s", url)
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _ha_post(self, path: str, data: dict | None = None) -> Any:
        session = await self._ensure_session()
        url = f"{HA_BASE_URL}{path}"
        log.debug("HA POST %s %s", url, data)
        async with session.post(url, json=data or {}) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _call_ha_service(
        self, entity_id: str, service: str, data: dict
    ) -> None:
        domain = _entity_domain(entity_id)
        service_data: dict[str, Any] = {"entity_id": entity_id}

        # Map panel fields to HA service data
        if "brightness" in data:
            service_data["brightness"] = int(data["brightness"])
        if "color_temp" in data:
            service_data["color_temp"] = int(data["color_temp"])
        if "rgb" in data:
            service_data["rgb_color"] = list(data["rgb"])
        if "speed" in data:
            service_data["percentage"] = int(data["speed"])

        path = f"/api/services/{domain}/{service}"
        try:
            await self._ha_post(path, service_data)
            log.info("Service call: %s/%s → %s", domain, service, entity_id)
        except Exception:
            log.exception("Failed to call service %s/%s for %s", domain, service, entity_id)

    # ------------------------------------------------------------------
    # Area registry
    # ------------------------------------------------------------------

    async def _fetch_areas(self) -> None:
        """Fetch area registry via the REST API (available on HA 2023.x+)."""
        try:
            # The config/area_registry/list endpoint is available via WS only
            # on some versions.  We try the REST template endpoint as fallback.
            session = await self._ensure_session()
            url = f"{HA_BASE_URL}/api/template"
            tpl = (
                "{% for area in areas() %}"
                '{"id":"{{ area }}","name":"{{ area_name(area) }}"},'
                "{% endfor %}"
            )
            async with session.post(url, json={"template": tpl}) as resp:
                if resp.status == 200:
                    raw = await resp.text()
                    # Template returns comma-separated JSON objects; wrap in array
                    raw = "[" + raw.rstrip().rstrip(",") + "]"
                    for entry in json.loads(raw):
                        self._areas[entry["id"]] = entry["name"]
                    log.info("Fetched %d areas from HA", len(self._areas))
        except Exception:
            log.debug("Could not fetch area registry (non-critical)", exc_info=True)

    async def _fetch_entity_area(self, entity_id: str) -> str:
        """Try to resolve entity → area via the entity registry REST API."""
        # The HA REST API does not expose a clean entity→area mapping.
        # We fall back to the area_id attribute provided in state objects by
        # some integrations, or area names populated via WS calls below.
        return ""

    # ------------------------------------------------------------------
    # Entity fetching & publishing
    # ------------------------------------------------------------------

    async def _fetch_all_states(self) -> list[dict]:
        return await self._ha_get("/api/states")

    async def _build_entity_list(self) -> dict[str, dict]:
        """Fetch all HA states and build the panel entity dict."""
        states = await self._fetch_all_states()
        await self._fetch_areas()
        entities: dict[str, dict] = {}

        for state_obj in states:
            eid: str = state_obj.get("entity_id", "")
            domain = _entity_domain(eid)

            if domain not in ("light", "fan", "switch", "sensor"):
                continue

            entry = _entity_to_panel_json(eid, state_obj)
            if entry is None:
                continue

            # Resolve area name
            attrs = state_obj.get("attributes", {})
            area_id = attrs.get("area_id", "")
            if area_id and area_id in self._areas:
                entry["area"] = self._areas[area_id]

            entities[eid] = entry

        return entities

    async def _publish_all_entities(self) -> None:
        """Fetch entities from HA and publish the full list to MQTT."""
        try:
            self._entities = await self._build_entity_list()
            payload = json.dumps(list(self._entities.values()), separators=(",", ":"))
            self._mqtt_publish(f"{TOPIC_PREFIX}/entities", payload, retain=True)
            log.info("Published %d entities to %s/entities", len(self._entities), TOPIC_PREFIX)
        except Exception:
            log.exception("Failed to publish entity list")

    def _publish_entity_state(self, entity_id: str, state_obj: dict) -> None:
        """Publish a single entity state update over MQTT."""
        entry = _entity_to_panel_json(entity_id, state_obj)
        if entry is None:
            return

        # Update area from cache
        area_id = state_obj.get("attributes", {}).get("area_id", "")
        if area_id and area_id in self._areas:
            entry["area"] = self._areas[area_id]

        self._entities[entity_id] = entry
        payload = json.dumps(entry, separators=(",", ":"))
        self._mqtt_publish(f"{TOPIC_PREFIX}/state", payload)
        log.debug("State update → %s", entity_id)

    # ------------------------------------------------------------------
    # HA WebSocket connection
    # ------------------------------------------------------------------

    async def _ws_connect(self) -> None:
        """Establish the HA WebSocket connection and subscribe to
        ``state_changed`` events.  Reconnects automatically on failure.
        """
        backoff = WS_RECONNECT_BASE
        while not self._shutdown_event.is_set():
            try:
                log.info("Connecting to HA WebSocket at %s", HA_WS_URL)
                async with websockets.connect(
                    HA_WS_URL,
                    additional_headers=_ha_headers(),
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    await self._ws_auth(ws)
                    await self._ws_subscribe_events(ws)
                    backoff = WS_RECONNECT_BASE
                    log.info("HA WebSocket authenticated & subscribed")

                    # Push full entity list once WS is ready
                    await self._publish_all_entities()

                    await self._ws_listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WebSocket error – reconnecting in %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_RECONNECT_MAX)

    async def _ws_auth(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Authenticate with the HA WebSocket API."""
        # First message is auth_required
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WS message: {msg}")

        await ws.send(
            json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN})
        )
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {msg}")

    async def _ws_subscribe_events(
        self, ws: websockets.WebSocketClientProtocol
    ) -> None:
        self._ws_id += 1
        await ws.send(
            json.dumps(
                {
                    "id": self._ws_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
        )
        result = json.loads(await ws.recv())
        if not result.get("success"):
            raise RuntimeError(f"WS subscribe failed: {result}")

    async def _ws_listen(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Listen for state_changed events and relay to MQTT."""
        async for raw in ws:
            if self._shutdown_event.is_set():
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") != "event":
                continue

            event = msg.get("event", {})
            if event.get("event_type") != "state_changed":
                continue

            data = event.get("data", {})
            entity_id: str = data.get("entity_id", "")
            new_state: dict | None = data.get("new_state")
            if not entity_id or new_state is None:
                continue

            domain = _entity_domain(entity_id)
            if domain not in ("light", "fan", "switch", "sensor"):
                continue

            self._publish_entity_state(entity_id, new_state)

    # ------------------------------------------------------------------
    # Optional status web server
    # ------------------------------------------------------------------

    async def _status_handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        """Simple JSON health endpoint."""
        payload = {
            "bridge": "online",
            "mqtt_connected": self._mqtt_connected,
            "panel_online": self._panel_online,
            "entities_tracked": len(self._entities),
            "timestamp": time.time(),
        }
        return aiohttp.web.json_response(payload)

    async def _start_status_server(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", self._status_handler)
        app.router.add_get("/status", self._status_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", STATUS_PORT)
        try:
            await site.start()
            log.info("Status server listening on port %d", STATUS_PORT)
        except Exception:
            log.warning("Could not start status server on port %d", STATUS_PORT, exc_info=True)

    # ------------------------------------------------------------------
    # Periodic discovery refresh
    # ------------------------------------------------------------------

    async def _periodic_discovery(self) -> None:
        """Re-publish full entity list at a configurable interval."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=DISCOVERY_INTERVAL
                )
                break  # shutdown was set
            except asyncio.TimeoutError:
                pass  # interval elapsed
            await self._publish_all_entities()

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        # --- MQTT (runs in a background thread via paho) ---
        self._mqtt = self._setup_mqtt()
        backoff = MQTT_RECONNECT_BASE
        while not self._mqtt_connected:
            try:
                log.info(
                    "Connecting MQTT to %s:%d (user=%s)",
                    MQTT_HOST,
                    MQTT_PORT,
                    MQTT_USER or "<none>",
                )
                self._mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self._mqtt.loop_start()
                # Give paho a moment to call on_connect
                for _ in range(50):
                    if self._mqtt_connected:
                        break
                    await asyncio.sleep(0.1)
                if self._mqtt_connected:
                    break
            except Exception:
                log.warning(
                    "MQTT connect failed – retrying in %ds", backoff, exc_info=True
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MQTT_RECONNECT_MAX)

        # --- Start tasks ---
        tasks = [
            asyncio.create_task(self._ws_connect(), name="ws"),
            asyncio.create_task(self._periodic_discovery(), name="discovery"),
            asyncio.create_task(self._start_status_server(), name="status"),
        ]

        # --- Graceful shutdown ---
        stop_signals = (signal.SIGTERM, signal.SIGINT)
        for sig in stop_signals:
            self._loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(self._shutdown(s))
            )

        # Wait until shutdown is signalled
        await self._shutdown_event.wait()

        # Cancel running tasks
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _shutdown(self, sig: signal.Signals | None = None) -> None:
        if sig:
            log.info("Received signal %s – shutting down", sig.name)
        self._shutdown_event.set()

        # Publish offline status
        if self._mqtt and self._mqtt_connected:
            self._mqtt.publish(
                f"{TOPIC_PREFIX}/bridge/status", "offline", qos=1, retain=True
            )
            self._mqtt.loop_stop()
            self._mqtt.disconnect()

        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info(
        "MyPanel Bridge starting (prefix=%s, discovery=%ds, log=%s)",
        TOPIC_PREFIX,
        DISCOVERY_INTERVAL,
        LOG_LEVEL,
    )
    bridge = MyPanelBridge()
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass
    log.info("MyPanel Bridge stopped")


if __name__ == "__main__":
    main()
