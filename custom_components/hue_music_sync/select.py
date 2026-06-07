"""Select entity: the music-sync Mode preset for an area."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SyncMode
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ModeSelect(manager, area_id) for area_id in manager.enabled_areas)


class ModeSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick a curated sync mode (bundles colour scheme + effect + intensity)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "mode"
    _attr_icon = "mdi:music-circle"
    _attr_options = [str(m) for m in SyncMode]

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "mode")

    @property
    def current_option(self) -> str:
        return str(self._manager.get_settings(self._area_id).mode)

    async def async_select_option(self, option: str) -> None:
        await self._manager.update_settings(self._area_id, mode=SyncMode(option))
        self.async_write_ha_state()
