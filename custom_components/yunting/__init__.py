"""云听电台集成：把云听（radio.cn）的直播电台推送到局域网 DLNA 渲染器。

云听客户端是纯标准库实现（阻塞式），所有调用都丢到执行器里跑，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import voluptuous as vol

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory

from homeassistant.components import media_source as hass_media_source
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .client import YuntingClient, YuntingError
from .const import (
    ATTR_ENTITY_ID,
    ATTR_STATION,
    CONF_LINE,
    CONF_QUALITY,
    CONF_STREAM_PREFIX,
    DEFAULT_LINE,
    DEFAULT_QUALITY,
    DEFAULT_STREAM_PREFIX,
    DOMAIN,
    LOGGER,
    RECENT_LIMIT,
    SERVICE_ADD_FAVORITE,
    SERVICE_CLEAR_RECENT,
    SERVICE_PUSH_STATION,
    SERVICE_REMOVE_FAVORITE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_PUSH_STATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_STATION): cv.string,
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)
# 不填 station 就作用于「当前正在播放的电台」
SERVICE_FAVORITE_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_STATION): cv.string}
)

# 设备报的流地址形如 ffmpeg://http://…/radios/22399/index_22399.m3u8?…
# 数字里前面是音质位，后面才是补零的 contentId，所以要逐位裁掉再比对
RADIOS_RE = re.compile(r'/radios/(\d+)')
STREAM_RE = re.compile(r'/stream_(\d+)')


class YuntingRuntime:
    """跨配置项共享的运行时数据。"""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        session = aiohttp_client.async_get_clientsession(hass, verify_ssl=False)
        self.requester = AiohttpSessionRequester(session, with_sleep=True)
        # 设备的服务描述里有非标准命名空间的 SCPD，必须容忍，否则根本连不上
        self.factory = UpnpFactory(self.requester, non_strict=True)
        self.client = YuntingClient(cache_dir=hass.config.path('.yunting-cache'))
        self.favorites: list[dict[str, Any]] = []
        self.recent: list[dict[str, Any]] = []
        # 刚从媒体浏览器解析出来的电台，用来在实体上显示台名（设备不回报元数据）
        self.last_resolved: dict[str, Any] | None = None
        # 已装载的媒体播放器实体 id
        self.entity_ids: list[str] = []
        # 推流偏好（由媒体播放器实体从配置项选项里带进来）
        self.stream_prefix = DEFAULT_STREAM_PREFIX
        self.quality = DEFAULT_QUALITY
        self.line = DEFAULT_LINE
        self._store: Store | None = None
        self._lock = asyncio.Lock()
        # 上一次应用的选项，用来判断配置项更新是不是真的改了选项（避免重载循环）
        self.applied_options: dict[str, Any] = {}

    def apply_options(self, options: dict[str, Any]) -> None:
        """应用配置项选项：是否加流前缀、音质与线路偏好。"""
        self.applied_options = dict(options)
        self.stream_prefix = (
            DEFAULT_STREAM_PREFIX if options.get(CONF_STREAM_PREFIX, True) else ''
        )
        self.quality = options.get(CONF_QUALITY) or DEFAULT_QUALITY
        self.line = options.get(CONF_LINE) or DEFAULT_LINE

    # ------------------------------------------------------------ 阻塞调用包装
    async def async_call(self, func, *args, **kwargs):
        """把阻塞的云听客户端调用丢进执行器。"""
        return await self.hass.async_add_executor_job(lambda: func(*args, **kwargs))

    # ---------------------------------------------------------------- 持久化
    async def async_load(self) -> None:
        self._store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
        data = await self._store.async_load() or {}
        self.favorites = data.get('favorites') or []
        self.recent = data.get('recent') or []

    async def _async_save(self) -> None:
        if self._store is None:
            return
        await self._store.async_save({'favorites': self.favorites, 'recent': self.recent})

    # ---------------------------------------------------------------- 收藏
    async def async_add_favorite(self, station: dict) -> bool:
        async with self._lock:
            if any(item['contentId'] == station['contentId'] for item in self.favorites):
                return False
            self.favorites.append({
                'contentId': station['contentId'],
                'title': station.get('title', ''),
                'tagName': station.get('tagName', ''),
            })
            await self._async_save()
            return True

    async def async_remove_favorite(self, key: str) -> int:
        async with self._lock:
            key = str(key).strip()
            kept = [item for item in self.favorites
                    if item['contentId'] != key and item['title'] != key]
            removed = len(self.favorites) - len(kept)
            if removed:
                self.favorites = kept
                await self._async_save()
            return removed

    # ---------------------------------------------------------------- 最近播放
    async def async_remember(self, station: dict) -> None:
        entry = {
            'contentId': station['contentId'],
            'title': station.get('title', ''),
            'tagName': station.get('tagName', ''),
            'at': int(time.time()),
        }
        self.last_resolved = entry
        async with self._lock:
            self.recent = [item for item in self.recent
                           if item['contentId'] != entry['contentId']]
            self.recent.insert(0, entry)
            del self.recent[RECENT_LIMIT:]
            await self._async_save()

    async def async_clear_recent(self) -> None:
        async with self._lock:
            self.recent = []
            await self._async_save()

    # ---------------------------------------------------------------- 工具
    def take_last_resolved(self, within: float = 10.0) -> dict | None:
        """取刚刚解析过的电台（短时间内有效），用于显示台名。"""
        entry = self.last_resolved
        if entry and time.time() - entry.get('at', 0) <= within:
            return entry
        return None


def get_runtime(hass: HomeAssistant) -> YuntingRuntime:
    """取出（必要时创建）运行时数据。"""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = YuntingRuntime(hass)
    return hass.data[DOMAIN]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """注册服务。"""
    runtime = get_runtime(hass)
    await runtime.async_load()

    async def _async_push(call: ServiceCall) -> None:
        station = await runtime.async_call(runtime.client.resolve, call.data[ATTR_STATION])
        await async_push_station(hass, station, call.data.get(ATTR_ENTITY_ID))

    async def _async_add_favorite(call: ServiceCall) -> None:
        station = await _async_station_from_call(hass, call)
        added = await runtime.async_add_favorite(station)
        LOGGER.info('收藏 %s：%s', station['title'], '已加入' if added else '本来就在')

    async def _async_remove_favorite(call: ServiceCall) -> None:
        station = await _async_station_from_call(hass, call)
        removed = await runtime.async_remove_favorite(station['contentId'])
        LOGGER.info('取消收藏 %s：%s 条', station['title'], removed)

    async def _async_clear_recent(call: ServiceCall) -> None:
        await runtime.async_clear_recent()

    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_STATION, _async_push, schema=SERVICE_PUSH_STATION_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_FAVORITE, _async_add_favorite, schema=SERVICE_FAVORITE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_FAVORITE, _async_remove_favorite, schema=SERVICE_FAVORITE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_RECENT, _async_clear_recent)

    return True


async def async_current_station(hass: HomeAssistant) -> dict | None:
    """当前正在播放的电台。

    优先用刚通过本集成推送的那台；否则从设备报的流地址里反推 contentId
    （这样用别的控制端推的台也认得出）。
    """
    runtime = get_runtime(hass)
    entry = runtime.take_last_resolved(within=600)
    if entry:
        return entry

    for entity_id in runtime.entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        uri = str(state.attributes.get('media_content_id') or '')
        match = RADIOS_RE.search(uri) or STREAM_RE.search(uri)
        if not match:
            continue
        digits = match.group(1)
        try:
            stations = await runtime.async_call(runtime.client.all_stations)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug('取电台列表失败：%s', err)
            return None
        by_id = {s['contentId']: s for s in stations}
        for cut in range(0, 3):
            candidate = digits[cut:].lstrip('0') or '0'
            if candidate in by_id:
                return by_id[candidate]
        LOGGER.debug('流地址 %s 没匹配到电台', uri)
    return None


async def _async_station_from_call(hass: HomeAssistant, call: ServiceCall) -> dict:
    """服务里的 station 参数：给了就解析，没给就用当前正在播放的那台。"""
    runtime = get_runtime(hass)
    if station := call.data.get(ATTR_STATION):
        return await runtime.async_call(runtime.client.resolve, station)
    current = await async_current_station(hass)
    if current is None:
        raise HomeAssistantError('认不出当前正在播放的电台，请在服务里指定 station')
    return current


async def async_push_station(hass: HomeAssistant, station: dict,
                             entity_ids: list[str] | None = None) -> None:
    """把电台推给媒体播放器实体（不指定就推给全部）。"""
    runtime = get_runtime(hass)
    targets = entity_ids or runtime.entity_ids
    if not targets:
        raise YuntingError('还没有可用的媒体播放器实体，请先添加设备')
    # 注意：这里必须用别名，否则会被本包的 media_source 子模块遮蔽
    media_id = hass_media_source.generate_media_source_id(
        DOMAIN, f'station/{station["contentId"]}')
    for entity_id in targets:
        await hass.services.async_call(
            'media_player', 'play_media',
            {
                'entity_id': entity_id,
                'media_content_id': media_id,
                'media_content_type': 'channel',
            },
            blocking=True,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """装载配置项。"""
    get_runtime(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载配置项。"""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """只在选项真的变了时重载。

    地址变化（连不上之后重新发现回写）也会触发这个监听，那种情况不需要重载 ——
    否则设备每次端口漂移都会把集成重载一遍，形成循环。
    """
    runtime = get_runtime(hass)
    if runtime.applied_options != dict(entry.options):
        await hass.config_entries.async_reload(entry.entry_id)


__all__ = ['DOMAIN', 'YuntingError', 'get_runtime', 'async_push_station']
