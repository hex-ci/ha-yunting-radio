"""按钮实体：一键收藏 / 取消收藏「当前正在播放的电台」。

HA 的媒体浏览器是只读浏览，不支持「对某个条目执行动作」，所以收藏做成按钮，
放进仪表盘点一下就行。当前电台的判定见 `__init__.async_current_station`。
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import async_current_station, get_runtime
from .const import CONF_UDN, DOMAIN, LOGGER

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """为每台设备加两个按钮。"""
    udn = entry.data[CONF_UDN]
    async_add_entities([
        YuntingFavoriteButton(hass, entry, udn, add=True),
        YuntingFavoriteButton(hass, entry, udn, add=False),
    ])


class YuntingFavoriteButton(ButtonEntity):
    """收藏 / 取消收藏当前电台。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, udn: str, add: bool) -> None:
        self.hass = hass
        self.entry = entry
        self._add = add
        self._attr_unique_id = f'{udn}_favorite' if add else f'{udn}_unfavorite'
        self._attr_translation_key = 'favorite_current' if add else 'unfavorite_current'
        self._attr_device_info = {'identifiers': {(DOMAIN, udn)}}

    async def async_press(self) -> None:
        """按一下：收藏 / 取消收藏当前电台。"""
        runtime = get_runtime(self.hass)
        station = await async_current_station(self.hass)
        if station is None:
            LOGGER.warning('认不出当前正在播放的电台，请用 yunting.add_favorite 指定台名')
            return
        if self._add:
            added = await runtime.async_add_favorite(station)
            LOGGER.info('收藏 %s：%s', station['title'], '已加入' if added else '本来就在')
        else:
            removed = await runtime.async_remove_favorite(station['contentId'])
            LOGGER.info('取消收藏 %s：%s 条', station['title'], removed)
