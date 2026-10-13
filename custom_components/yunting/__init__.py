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
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .client import YuntingClient, YuntingError
from .discovery import async_find_location_by_udn
from .const import (
    ATTR_ENTITY_ID,
    ATTR_STATION,
    AV_TRANSPORT_SERVICE,
    REDISCOVERY_COOLDOWN,
    CONF_LINE,
    CONF_QUALITY,
    CONF_SOURCE_ONLY,
    CONF_STREAM_PREFIX,
    CONF_UDN,
    CONF_URL,
    DEFAULT_LINE,
    DEFAULT_QUALITY,
    DEFAULT_STREAM_PREFIX,
    DOMAIN,
    LOGGER,
    RECENT_LIMIT,
    SERVICE_ADD_FAVORITE,
    SERVICE_CLEAR_RECENT,
    SERVICE_PUSH_STATION,
    SERVICE_REBOOT,
    SERVICE_REDISCOVER,
    SERVICE_REMOVE_FAVORITE,
    SERVICE_SHUTDOWN,
    STORAGE_KEY,
    STORAGE_VERSION,
    TIMEOUT_REBOOT,
    TIMEOUT_SHUTDOWN,
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
        # 每台设备上次「按 UDN 重发现」的时间，用来给重发现降温
        self._rediscovered_at: dict[str, float] = {}
        # 已装载的媒体播放器实体 id
        self.entity_ids: list[str] = []
        # 推流偏好（由媒体播放器实体从配置项选项里带进来）
        self.stream_prefix = DEFAULT_STREAM_PREFIX
        self.quality = DEFAULT_QUALITY
        self.line = DEFAULT_LINE
        self._store: Store | None = None
        self._lock = asyncio.Lock()
        # 每个配置项一份 UPnP 设备连接（按钮/传感器用）
        self._devices: dict[str, Any] = {}
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

    # ------------------------------------------------------------ 设备动作
    async def async_device(self, entry: ConfigEntry):
        """取（必要时建立）配置项对应的 UPnP 设备连接；地址变了会重发现。"""
        cached = self._devices.get(entry.entry_id)
        if cached is not None:
            return cached

        url = entry.data.get(CONF_URL)
        try:
            device = await self.factory.async_create_device(url)
        except Exception as err:  # noqa: BLE001
            new_url = await self.async_rediscover(entry)
            if not new_url or new_url == url:
                raise YuntingError(f'连不上设备：{url}') from err
            device = await self.factory.async_create_device(new_url)
            LOGGER.info('设备地址已变化：%s → %s', url, new_url)
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_URL: new_url})

        self._devices[entry.entry_id] = device
        return device

    async def async_rediscover(self, entry: ConfigEntry, force: bool = False) -> str | None:
        """按 UDN 重新发现设备地址，默认带冷却。

        后台轮询**不再调用**这里（设备常年关机，SSDP 搜索每次 6–8 秒纯属浪费）：
        只有用户主动操作才搜——推送播放、点「重新发现设备」按钮、调 `yunting.rediscover`。
        `force=True`（手动按钮/服务）时忽略冷却，用户点一次就真搜一次。
        """
        udn = entry.data.get(CONF_UDN)
        if not udn:
            return None
        now = time.monotonic()
        last = self._rediscovered_at.get(udn)
        if not force and last is not None and now - last < REDISCOVERY_COOLDOWN:
            LOGGER.debug('距上次重发现不足 %s 秒，跳过', REDISCOVERY_COOLDOWN)
            return None
        self._rediscovered_at[udn] = now
        return await async_find_location_by_udn(self.hass, udn)

    def async_forget_device(self, entry: ConfigEntry) -> None:
        """丢掉缓存的设备连接，下次调用重新连。"""
        self._devices.pop(entry.entry_id, None)

    async def async_call_action(self, entry: ConfigEntry, action: str,
                                service: str = AV_TRANSPORT_SERVICE, **args) -> dict:
        """在设备上调用一个 UPnP 动作；失败就丢缓存，下次重新连。"""
        device = await self.async_device(entry)
        try:
            upnp_service = device.service(service)
            upnp_action = upnp_service.action(action)
            if upnp_action is None:
                raise YuntingError(f'设备不支持动作 {action}')
            return await upnp_action.async_call(**args)
        except Exception:
            self.async_forget_device(entry)
            raise

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

    async def _async_reboot(call: ServiceCall) -> None:
        await _async_power(hass, TIMEOUT_REBOOT, '重启')

    async def _async_shutdown(call: ServiceCall) -> None:
        await _async_power(hass, TIMEOUT_SHUTDOWN, '关机')

    async def _async_rediscover(call: ServiceCall) -> dict[str, Any]:
        return await _async_rediscover_all(hass)

    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_STATION, _async_push, schema=SERVICE_PUSH_STATION_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_FAVORITE, _async_add_favorite, schema=SERVICE_FAVORITE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_FAVORITE, _async_remove_favorite, schema=SERVICE_FAVORITE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_RECENT, _async_clear_recent)
    hass.services.async_register(DOMAIN, SERVICE_REBOOT, _async_reboot)
    hass.services.async_register(DOMAIN, SERVICE_SHUTDOWN, _async_shutdown)
    hass.services.async_register(
        DOMAIN, SERVICE_REDISCOVER, _async_rediscover,
        supports_response=SupportsResponse.OPTIONAL)

    return True


async def _async_rediscover_all(hass: HomeAssistant) -> dict[str, Any]:
    """主动重发现所有设备的地址（按钮/服务走的就是这里），搜到的地址回写配置项。"""
    runtime = get_runtime(hass)
    entries = _device_entries(hass)
    if not entries:
        raise HomeAssistantError('还没有添加任何设备，没有地址可发现')

    found: dict[str, Any] = {}
    for entry in entries:
        old_url = entry.data.get(CONF_URL)
        new_url = await runtime.async_rediscover(entry, force=True)
        if not new_url:
            LOGGER.info('重新发现：没搜到「%s」', entry.title)
            found[entry.title] = None
            continue
        if new_url != old_url:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_URL: new_url})
            LOGGER.info('重新发现：%s 地址已更新 %s → %s', entry.title, old_url, new_url)
        else:
            LOGGER.info('重新发现：%s 地址没变（%s）', entry.title, new_url)
        found[entry.title] = new_url
    return {'devices': found}


async def _async_power(hass: HomeAssistant, timeout: int, label: str) -> None:
    """给所有本集成的设备下发重启/关机（自动化用，不经按钮的二次确认）。"""
    runtime = get_runtime(hass)
    entries = _device_entries(hass)
    if not entries:
        raise HomeAssistantError('还没有添加任何设备，没有可操作的音响')
    for entry in entries:
        try:
            await runtime.async_call_action(
                entry, 'TimingShutdown', service=AV_TRANSPORT_SERVICE,
                InstanceID=0, TimeOuts=timeout)
        except Exception as err:  # noqa: BLE001
            LOGGER.warning('下发%s失败：%s', label, err)
            raise HomeAssistantError(f'下发{label}失败：{err}') from err
        LOGGER.info('已下发%s指令（%s）', label, entry.title)


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
    """装载配置项。

    「只启用内容源」的配置项（没有设备）不装载任何实体平台：它存在的意义就是让
    本集成处于已装载状态，从而把云听电台注册进 HA 的媒体库。
    """
    get_runtime(hass)
    if entry.data.get(CONF_SOURCE_ONLY):
        LOGGER.info('云听电台内容源已启用（这台没添加设备）')
        return True
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _device_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """本集成里真正带设备的配置项（有 UDN 的）；「只启用内容源」的那种不算。"""
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_UDN)]


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载配置项。"""
    if entry.data.get(CONF_SOURCE_ONLY):
        return True
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
