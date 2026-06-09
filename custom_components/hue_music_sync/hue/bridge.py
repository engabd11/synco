"""Hue CLIP v2 client: pairing and entertainment configuration management."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

from ..const import DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)

_API_TIMEOUT = aiohttp.ClientTimeout(total=10)


def capture_light_state(light: dict) -> dict:
    """Snapshot the restorable state of a CLIP v2 light resource.

    Keeps on/off and brightness, and exactly one colour mode — the colour
    temperature if the light is currently in white/CT mode, otherwise its xy
    chromaticity — so the light can be put back exactly as it was.
    """
    state: dict = {"id": light["id"], "on": light.get("on", {}).get("on", True)}
    brightness = light.get("dimming", {}).get("brightness")
    if brightness is not None:
        state["brightness"] = brightness
    ct = light.get("color_temperature") or {}
    mirek = ct.get("mirek")
    if mirek is not None:
        state["mirek"] = mirek  # light was in CT/white mode
    else:
        xy = light.get("color", {}).get("xy")
        if xy is not None:
            state["xy"] = xy
    return state


def restore_light_body(state: dict) -> dict:
    """Build the CLIP v2 PUT body that puts a light back to a captured state."""
    body: dict = {"on": {"on": state["on"]}}
    if "brightness" in state:
        body["dimming"] = {"brightness": state["brightness"]}
    if "mirek" in state:
        body["color_temperature"] = {"mirek": state["mirek"]}
    elif "xy" in state:
        body["color"] = {"xy": state["xy"]}
    return body


class LinkButtonNotPressed(Exception):
    """Raised during pairing when the bridge link button has not been pressed."""


class HueBridgeError(Exception):
    """Generic bridge/API error."""


@dataclass(slots=True)
class EntertainmentChannel:
    """One streamable channel within an entertainment configuration."""

    channel_id: int
    x: float
    y: float
    z: float


@dataclass(slots=True)
class EntertainmentConfig:
    """An entertainment area as exposed by CLIP v2."""

    id: str
    name: str
    status: str
    channels: list[EntertainmentChannel] = field(default_factory=list)

    @property
    def is_streaming(self) -> bool:
        return self.status == "active"


async def create_app_key(
    session: aiohttp.ClientSession, host: str, ssl_ctx
) -> tuple[str, str]:
    """Pair with the bridge, returning ``(app_key, client_key)``.

    The link button on the bridge must have been pressed within the last ~30s.
    Uses the legacy ``/api`` endpoint, which still mints the ``clientkey`` (PSK)
    needed for entertainment streaming.
    """
    url = f"https://{host}/api"
    payload = {"devicetype": DEFAULT_NAME, "generateclientkey": True}
    async with session.post(url, json=payload, ssl=ssl_ctx, timeout=_API_TIMEOUT) as resp:
        data = await resp.json(content_type=None)

    if not isinstance(data, list) or not data:
        raise HueBridgeError(f"Unexpected pairing response: {data!r}")
    entry = data[0]
    if "error" in entry:
        err = entry["error"]
        if err.get("type") == 101:
            raise LinkButtonNotPressed
        raise HueBridgeError(err.get("description", str(err)))
    success = entry.get("success", {})
    app_key = success.get("username")
    client_key = success.get("clientkey")
    if not app_key or not client_key:
        raise HueBridgeError(f"Pairing succeeded but key missing: {success!r}")
    return app_key, client_key


class HueBridge:
    """Authenticated CLIP v2 client for one bridge."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        app_key: str,
        ssl_ctx,
    ) -> None:
        self._session = session
        self._host = host
        self._app_key = app_key
        self._ssl = ssl_ctx

    @property
    def _headers(self) -> dict[str, str]:
        return {"hue-application-key": self._app_key}

    def _url(self, path: str) -> str:
        return f"https://{self._host}/clip/v2/resource/{path}"

    async def _get(self, path: str) -> list[dict]:
        async with self._session.get(
            self._url(path), headers=self._headers, ssl=self._ssl, timeout=_API_TIMEOUT
        ) as resp:
            if resp.status == 403:
                raise HueBridgeError("Bridge rejected the application key (403)")
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        if body.get("errors"):
            raise HueBridgeError(str(body["errors"]))
        return body.get("data", [])

    async def get_entertainment_configs(self) -> list[EntertainmentConfig]:
        """List entertainment areas with their channel positions."""
        configs: list[EntertainmentConfig] = []
        for item in await self._get("entertainment_configuration"):
            channels = [
                EntertainmentChannel(
                    channel_id=ch["channel_id"],
                    x=ch.get("position", {}).get("x", 0.0),
                    y=ch.get("position", {}).get("y", 0.0),
                    z=ch.get("position", {}).get("z", 0.0),
                )
                for ch in item.get("channels", [])
            ]
            configs.append(
                EntertainmentConfig(
                    id=item["id"],
                    name=item.get("metadata", {}).get("name", item["id"]),
                    status=item.get("status", "inactive"),
                    channels=channels,
                )
            )
        return configs

    async def get_entertainment_config(self, config_id: str) -> EntertainmentConfig:
        for cfg in await self.get_entertainment_configs():
            if cfg.id == config_id:
                return cfg
        raise HueBridgeError(f"Entertainment configuration {config_id} not found")

    async def _set_action(self, config_id: str, action: str) -> None:
        url = self._url(f"entertainment_configuration/{config_id}")
        async with self._session.put(
            url,
            headers=self._headers,
            json={"action": action},
            ssl=self._ssl,
            timeout=_API_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        if body.get("errors"):
            raise HueBridgeError(str(body["errors"]))

    async def start_stream(self, config_id: str) -> None:
        """Hand the entertainment area over to streaming mode."""
        await self._set_action(config_id, "start")

    async def stop_stream(self, config_id: str) -> None:
        """Return control of the area to the bridge (restores prior light state)."""
        await self._set_action(config_id, "stop")

    async def _put(self, path: str, body: dict) -> None:
        async with self._session.put(
            self._url(path), headers=self._headers, json=body,
            ssl=self._ssl, timeout=_API_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if data.get("errors"):
            raise HueBridgeError(str(data["errors"]))

    async def _area_light_rids(self, config_id: str) -> set[str]:
        """Resolve the light resource ids that belong to an entertainment area."""
        items = await self._get(f"entertainment_configuration/{config_id}")
        item = items[0] if items else {}
        # Some bridges still expose the direct (deprecated) light_services list.
        rids = {
            s["rid"]
            for s in item.get("light_services", [])
            if s.get("rtype") == "light"
        }
        if rids:
            return rids
        # Otherwise map channel members -> entertainment services -> owner
        # devices -> the light service on each device.
        ent_rids = {
            m["service"]["rid"]
            for ch in item.get("channels", [])
            for m in ch.get("members", [])
            if m.get("service", {}).get("rtype") == "entertainment"
        }
        if not ent_rids:
            return set()
        device_rids = {
            e["owner"]["rid"]
            for e in await self._get("entertainment")
            if e.get("id") in ent_rids and e.get("owner", {}).get("rtype") == "device"
        }
        return {
            light["id"]
            for light in await self._get("light")
            if light.get("owner", {}).get("rid") in device_rids
        }

    async def snapshot_area_lights(self, config_id: str) -> list[dict]:
        """Capture the current state of every light in an entertainment area."""
        rids = await self._area_light_rids(config_id)
        if not rids:
            return []
        return [
            capture_light_state(light)
            for light in await self._get("light")
            if light["id"] in rids
        ]

    async def restore_light_states(self, states: list[dict], passes: int = 2) -> None:
        """Put each light back to a captured state, retrying to beat Zigbee loss.

        The bridge's own restore-on-stop occasionally drops a single light's
        command, so we re-apply our snapshot ourselves a couple of times.
        """
        for attempt in range(passes):
            for state in states:
                try:
                    await self._put(f"light/{state['id']}", restore_light_body(state))
                except (HueBridgeError, OSError) as err:
                    _LOGGER.debug("Restore of light %s failed: %s", state.get("id"), err)
            if attempt < passes - 1:
                await asyncio.sleep(0.3)
