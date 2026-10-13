"""云听电台的媒体播放器实体（DLNA 渲染器 + 媒体浏览器搜索）。

为什么不用官方 dlna_dmr：它不声明 SEARCH_MEDIA，HA 的媒体浏览器就不会给搜索框，
而「搜云听电台再推送」正是这个集成的目的。所以这里自己实现媒体播放器，
底层用与 dlna_dmr 相同的 async_upnp_client。
"""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from async_upnp_client.exceptions import UpnpConnectionError, UpnpError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from didl_lite import didl_lite

from homeassistant.components import media_source
from homeassistant.components import persistent_notification
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.media_player.browse_media import (
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import get_runtime
from .const import (
    ACTION_FAVORITE,
    ACTION_UNFAVORITE,
    CONF_UDN,
    CONF_URL,
    DOMAIN,
    LOGGER,
    NODE_ACTION_PREFIX,
    NODE_ROOT,
    SCAN_INTERVAL_SECONDS,
)

SCAN_INTERVAL = timedelta(seconds=SCAN_INTERVAL_SECONDS)

PARALLEL_UPDATES = 1

# 动作条目：media-source://yunting/action/favorite/1001
ACTION_RE = re.compile(
    rf'^media-source://{DOMAIN}/{NODE_ACTION_PREFIX}'
    rf'({ACTION_FAVORITE}|{ACTION_UNFAVORITE})/(\d+)$'
)

TRANSPORT_STATE_MAP = {
    TransportState.STOPPED: MediaPlayerState.IDLE,
    TransportState.PLAYING: MediaPlayerState.PLAYING,
    TransportState.TRANSITIONING: MediaPlayerState.PLAYING,
    TransportState.PAUSED_PLAYBACK: MediaPlayerState.PAUSED,
    TransportState.NO_MEDIA_PRESENT: MediaPlayerState.IDLE,
}

# 设备不校验 DIDL，用标准写法即可
PROTOCOL_INFO = 'http-get:*:audio/mpeg:*'

RC_SERVICE_TYPE = 'urn:schemas-upnp-org:service:RenderingControl:1'


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """装载媒体播放器实体。"""
    runtime = get_runtime(hass)
    runtime.apply_options(dict(entry.options))
    async_add_entities([YuntingRenderer(hass, entry)])


class YuntingRenderer(MediaPlayerEntity):
    """一台 DLNA 渲染器 + 云听内容浏览。"""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._url: str = entry.data[CONF_URL]
        self._udn: str = entry.data[CONF_UDN]
        self._dmr: DmrDevice | None = None
        self._connect_lock = asyncio.Lock()
        self._pushed_title: str | None = None
        self._volume: float | None = None

        self._attr_unique_id = self._udn
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self._udn)},
            connections={(dr.CONNECTION_UPNP, self._udn)},
            name=entry.title,
            manufacturer='GEAK',
        )

    # ------------------------------------------------------------ 生命周期
    async def async_added_to_hass(self) -> None:
        """加入后连接设备，并登记实体 id（服务推送要用）。"""
        runtime = get_runtime(self.hass)
        if self.entity_id not in runtime.entity_ids:
            runtime.entity_ids.append(self.entity_id)
        self.async_on_remove(self._async_forget_entity)
        # 连接丢后台：设备离线时同步连要等连接超时 + 重发现（10 秒以上），会拖慢 HA 启动。
        # 第一个轮询周期也会连，所以这里不阻塞不影响功能，只是设备在线时能更快变可用。
        self.hass.async_create_task(self._async_connect())

    def _async_forget_entity(self) -> None:
        runtime = get_runtime(self.hass)
        if self.entity_id in runtime.entity_ids:
            runtime.entity_ids.remove(self.entity_id)

    async def _async_connect(self) -> bool:
        """（重新）建立与设备的连接；地址变了就自己找回来。"""
        runtime = get_runtime(self.hass)
        async with self._connect_lock:
            if await self._async_try_connect(self._url):
                return True

            # 连不上：设备可能重启过 UPnP 服务，端口会漂（实测 49152 / 49153 / 49154 都出现过）
            new_url = await runtime.async_rediscover(self.entry)
            if new_url and new_url != self._url and await self._async_try_connect(new_url):
                LOGGER.info('设备地址已变化：%s → %s', self._url, new_url)
                self._url = new_url
                # 回写配置项，重启 HA 之后也不用再找一遍
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, CONF_URL: new_url})
                return True

            self._attr_available = False
            return False

    async def _async_try_connect(self, url: str) -> bool:
        """按给定地址建连接（不动配置）。"""
        runtime = get_runtime(self.hass)
        try:
            device = await runtime.factory.async_create_device(url)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug('连接 %s 失败：%s', url, err)
            return False
        self._dmr = DmrDevice(device, None)
        self._attr_available = True
        LOGGER.debug('已连接 %s', url)
        return True

    async def async_update(self) -> None:
        """轮询状态。"""
        if self._dmr is None and not await self._async_connect():
            return
        assert self._dmr is not None
        try:
            await self._dmr.async_update(do_ping=True)
            self._attr_available = True
        except UpnpConnectionError:
            # 设备重启过 UPnP 服务（端口会变），下一轮重连
            LOGGER.debug('设备不可达，准备重连：%s', self._url)
            self._dmr = None
            self._attr_available = False
        except Exception as err:  # noqa: BLE001
            # 设备对直播流把 TrackMetaData / CurrentURIMetaData 回成字面量 unkown，
            # async_upnp_client 会拿它当 XML 解析并抛 ParseError。状态变量本身已经更新，
            # 所以这里只记日志，不要把整个实体打成不可用。
            LOGGER.debug('读取设备状态时有异常（多半是设备返回的元数据不是合法 XML）：%s', err)

        await self._async_poll_volume()

    async def _async_poll_volume(self) -> None:
        """自己读一次音量。

        DmrDevice 的轮询顺序里 GetVolume 排在元数据解析之后，上面那个 ParseError 会把
        它整段跳过（表现就是音量永远为空）。所以音量单独用公开 API 读，不吃它的缓存。
        """
        if self._dmr is None:
            return
        try:
            service = self._dmr.profile_device.service(RC_SERVICE_TYPE)
            result = await service.action('GetVolume').async_call(
                InstanceID=0, Channel='Master')
            self._volume = int(result['CurrentVolume']) / 100
        except Exception as err:  # noqa: BLE001
            LOGGER.debug('读音量失败：%s', err)

    # -------------------------------------------------------------- 状态
    @property
    def available(self) -> bool:
        return self._attr_available and self._dmr is not None

    @property
    def state(self) -> MediaPlayerState | None:
        if self._dmr is None:
            return None
        return TRANSPORT_STATE_MAP.get(self._dmr.transport_state, MediaPlayerState.IDLE)

    @property
    def volume_level(self) -> float | None:
        """音量 0..1：优先用自己读到的值。"""
        if self._volume is not None:
            return self._volume
        return self._dmr.volume_level if self._dmr else None

    @property
    def is_volume_muted(self) -> bool | None:
        return self._dmr.is_volume_muted if self._dmr else None

    @property
    def media_title(self) -> str | None:
        """台名：设备不回报元数据，所以优先用我们推的时候记下来的。"""
        if self._pushed_title:
            return self._pushed_title
        return self._dmr.media_title if self._dmr else None

    @property
    def media_content_id(self) -> str | None:
        return self._dmr.current_track_uri if self._dmr else None

    @property
    def media_position(self) -> int | None:
        return self._dmr.media_position if self._dmr else None

    @property
    def media_position_updated_at(self):
        return self._dmr.media_position_updated_at if self._dmr else None

    @property
    def media_duration(self) -> int | None:
        return self._dmr.media_duration if self._dmr else None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.SEEK
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            # 关键：声明了它，HA 的媒体浏览器才会出现搜索框
            | MediaPlayerEntityFeature.SEARCH_MEDIA
        )

    # -------------------------------------------------------------- 控制
    async def _async_call(self, action: str, method: str, *args) -> None:
        """调用设备动作，顺手处理断连。"""
        if self._dmr is None and not await self._async_connect():
            raise HomeAssistantError(f'设备不可用，无法执行「{action}」')
        assert self._dmr is not None
        try:
            await getattr(self._dmr, method)(*args)
        except UpnpError as err:
            self._dmr = None
            raise HomeAssistantError(f'执行「{action}」失败：{err}') from err
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        await self._async_call('播放', 'async_play')

    async def async_media_pause(self) -> None:
        await self._async_call('暂停', 'async_pause')

    async def async_media_stop(self) -> None:
        await self._async_call('停止', 'async_stop')

    async def async_media_next_track(self) -> None:
        await self._async_call('下一个', 'async_next')

    async def async_media_previous_track(self) -> None:
        await self._async_call('上一个', 'async_previous')

    async def async_media_seek(self, position: float) -> None:
        await self._async_call('跳转', 'async_seek_rel_time', int(position))

    async def async_set_volume_level(self, volume: float) -> None:
        await self._async_call('设置音量', 'async_set_volume_level', volume)
        self._volume = volume

    async def async_mute_volume(self, mute: bool) -> None:
        await self._async_call('静音', 'async_mute_volume', mute)

    # ---------------------------------------------------------- 动作条目
    async def _async_run_action(self, action: str, station_id: str) -> None:
        """执行媒体浏览器里点到的动作条目（收藏 / 取消收藏），不推流。"""
        runtime = get_runtime(self.hass)
        station = await runtime.async_call(runtime.client.resolve, station_id)
        title = station.get('title') or station_id
        if action == ACTION_FAVORITE:
            added = await runtime.async_add_favorite(station)
            message = f'已收藏「{title}」' if added else f'「{title}」已经在收藏里'
        else:
            removed = await runtime.async_remove_favorite(station_id)
            message = f'已取消收藏「{title}」' if removed else f'「{title}」本来就不在收藏里'
        LOGGER.info('%s', message)
        # 点动作条目本身不会改变播放状态，给一条通知做反馈（同一个 id 覆盖，不会堆）
        persistent_notification.async_create(
            self.hass, message, title='云听电台',
            notification_id=f'{DOMAIN}_favorite_action')

    # ---------------------------------------------------------- 播放媒体
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs
    ) -> None:
        """播放一个媒体项：媒体源里的电台节点，或任何直链。"""
        runtime = get_runtime(self.hass)
        title = ''

        # 动作条目（收藏 / 取消收藏）：执行动作，不推流
        if (action := ACTION_RE.match(media_id)) is not None:
            await self._async_run_action(action.group(1), action.group(2))
            return

        if media_source.is_media_source_id(media_id):
            sourced = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id)
            media_id = sourced.url
            entry = runtime.take_last_resolved()
            title = (entry or {}).get('title', '')

        media_id = async_process_play_media_url(self.hass, media_id)

        # 果壳音响这类设备要求直播流地址带前缀，裸 http 会被接受但不出声
        if runtime.stream_prefix and media_id.startswith(('http://', 'https://')):
            media_id = f'{runtime.stream_prefix}{media_id}'
        LOGGER.debug('准备推送的地址：%s', media_id)

        if self._dmr is None and not await self._async_connect():
            raise HomeAssistantError('设备不可用，无法推送')

        didl_metadata = didl_lite.to_xml_string(
            didl_lite.MusicTrack(
                id='0',
                parent_id='-1',
                restricted='false',
                title=title or '云听电台',
                resources=[didl_lite.Resource(uri=media_id, protocol_info=PROTOCOL_INFO)],
            )
        ).decode()

        try:
            if self._dmr.can_stop:
                await self._dmr.async_stop()
            await self._dmr.async_set_transport_uri(media_id, title or '云听电台',
                                                    didl_metadata)
            await self._dmr.async_play()
        except UpnpError as err:
            self._dmr = None
            raise HomeAssistantError(f'推送失败：{err}') from err

        self._pushed_title = title or None
        LOGGER.debug('已推送：%s（%s）', title or '未知电台', media_id)
        self.async_write_ha_state()

    # ---------------------------------------------------------- 媒体浏览
    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """媒体浏览器：默认直接进云听这棵树（而不是所有媒体源的列表）。

        这样点开这台音响就是「央广频率 / 按省份 / 我的收藏 / 最近播放」加一个搜索框；
        想放本地媒体或 TTS 时走树里的「其他媒体源」。
        """
        if not media_content_id:
            media_content_id = media_source.generate_media_source_id(DOMAIN, NODE_ROOT)
        return await media_source.async_browse_media(self.hass, media_content_id)

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """媒体浏览器的搜索框：转给媒体源搜全国电台。"""
        media_content_id = query.media_content_id or media_source.generate_media_source_id(
            DOMAIN, NODE_ROOT)
        return await media_source.async_search_media(self.hass, media_content_id, query)
