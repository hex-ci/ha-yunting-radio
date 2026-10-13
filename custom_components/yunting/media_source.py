"""云听电台媒体源：在 HA 的媒体浏览器里浏览、搜索、播放电台。"""
from __future__ import annotations

from homeassistant.components import media_source as hass_media_source
from homeassistant.components.media_player import BrowseMedia, MediaClass, MediaType
from homeassistant.components.media_player.browse_media import SearchMedia, SearchMediaQuery
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.core import HomeAssistant

from . import get_runtime
from .const import (
    ACTION_FAVORITE,
    ACTION_UNFAVORITE,
    DOMAIN,
    LOGGER,
    NODE_ACTION_PREFIX,
    NODE_ALL_SOURCES,
    NODE_FAVORITES,
    NODE_NATIONAL,
    NODE_PROVINCES,
    NODE_PROVINCE_PREFIX,
    NODE_RECENT,
    NODE_ROOT,
    NODE_STATION_PREFIX,
)


async def async_get_media_source(hass: HomeAssistant) -> 'YuntingMediaSource':
    """媒体源入口。"""
    return YuntingMediaSource(hass)


class YuntingMediaSource(MediaSource):
    """云听电台的内容源。"""

    name = '云听电台'

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ------------------------------------------------------------------ 浏览
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """按层级浏览。"""
        runtime = get_runtime(self.hass)
        identifier = item.identifier or NODE_ROOT

        if not identifier:
            return self._directory(
                identifier=NODE_ROOT,
                title=self.name,
                children=[
                    self._directory(NODE_NATIONAL, '央广频率', can_expand=True),
                    self._directory(NODE_PROVINCES, '按省份', can_expand=True),
                    self._directory(NODE_FAVORITES, '我的收藏', can_expand=True),
                    self._directory(NODE_RECENT, '最近播放', can_expand=True),
                    self._directory(NODE_ALL_SOURCES, '其他媒体源', can_expand=True),
                ],
            )

        if identifier == NODE_ALL_SOURCES:
            # 本地媒体、TTS 等其他媒体源仍可从这棵树下进入
            return await hass_media_source.async_browse_media(self.hass, None)

        if identifier == NODE_NATIONAL:
            stations = await runtime.async_call(runtime.client.national)
            return self._directory(
                identifier, '央广频率',
                children=[self._station_node(s) for s in stations])

        if identifier == NODE_PROVINCES:
            provinces = await runtime.async_call(runtime.client.provinces)
            return self._directory(
                identifier, '按省份',
                children=[
                    self._directory(f'{NODE_PROVINCE_PREFIX}{p["provinceCode"]}',
                                    p['provinceName'], can_expand=True)
                    for p in provinces
                ])

        if identifier.startswith(NODE_PROVINCE_PREFIX):
            code = identifier[len(NODE_PROVINCE_PREFIX):]
            stations = await runtime.async_call(runtime.client.by_province, code)
            return self._directory(
                identifier, f'省份 {code}',
                children=[self._station_node(s) for s in stations])

        if identifier == NODE_FAVORITES:
            return self._directory(
                identifier, '我的收藏',
                children=[self._station_node(s) for s in runtime.favorites])

        if identifier == NODE_RECENT:
            return self._directory(
                identifier, '最近播放',
                children=[self._station_node(s) for s in runtime.recent])

        if identifier.startswith(NODE_STATION_PREFIX):
            station = await self._async_station(identifier[len(NODE_STATION_PREFIX):])
            return self._station_node(station)

        raise Unresolvable(f'未知的节点：{identifier}')

    # ------------------------------------------------------------------ 搜索
    async def async_search_media(self, item: MediaSourceItem,
                                 query: SearchMediaQuery) -> SearchMedia:
        """按关键词搜全国电台（本地索引，941 个）。"""
        keyword = (query.search_query or '').strip()
        if not keyword:
            return SearchMedia(result=[])
        runtime = get_runtime(self.hass)
        try:
            stations = await runtime.async_call(runtime.client.search, keyword, 50)
        except Exception as err:  # noqa: BLE001
            LOGGER.warning('搜索「%s」失败：%s', keyword, err)
            return SearchMedia(result=[])
        LOGGER.debug('搜索「%s」命中 %s 个', keyword, len(stations))
        return SearchMedia(result=[self._station_node(s) for s in stations])

    # ------------------------------------------------------------------ 解析
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """把电台节点解析成可直接播放的地址（现取，带时效令牌）。"""
        identifier = item.identifier or ''
        if identifier.startswith(NODE_ACTION_PREFIX):
            # 动作条目由媒体播放器实体拦截执行，正常不会走到这里
            raise Unresolvable('这是动作条目，不是可播放的电台')
        if not identifier.startswith(NODE_STATION_PREFIX):
            raise Unresolvable(f'无法播放：{identifier}')

        runtime = get_runtime(self.hass)
        content_id = identifier[len(NODE_STATION_PREFIX):]
        station = await self._async_station(content_id)
        try:
            url = await runtime.async_call(
                runtime.client.fresh_url, content_id,
                quality=runtime.quality, line=runtime.line)
        except Exception as err:  # noqa: BLE001
            raise Unresolvable(f'取「{station["title"]}」的播放地址失败：{err}') from err

        await runtime.async_remember(station)
        # 只给「裸」地址：`ffmpeg://` 之类的前缀是**播放设备**的特性，由播放端加
        # （我们的播放器实体会加），内容源不该替别的设备做决定。
        return PlayMedia(url=url, mime_type='audio/mpeg')

    # ------------------------------------------------------------------ 工具
    async def _async_station(self, content_id: str) -> dict:
        runtime = get_runtime(self.hass)
        return await runtime.async_call(runtime.client.resolve, content_id)

    def _directory(self, identifier: str, title: str, *,
                   children: list[BrowseMedia] | None = None,
                   can_expand: bool = True) -> BrowseMediaSource:
        """目录节点。每个节点都开搜索，用户在任意层级都能直接搜。

        `media_content_type` 必须是字符串：前端把节点的这个值原样回传，
        而 HA 的 `media_player/browse_media` 接口要求它非空（传 None 会报
        「expected str at 'media_content_type'. Got None」）。
        """
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier or None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.CHANNELS,
            title=title,
            can_play=False,
            can_expand=can_expand,
            can_search=True,
            children_media_class=MediaClass.CHANNEL,
            children=children,
        )

    def _station_node(self, station: dict) -> BrowseMediaSource:
        """电台条目：行尾 ▶ 直接播；点整行会展开出「收藏 / 取消收藏」两个动作。

        HA 的媒体浏览器没有给条目挂自定义按钮的机制，只有「播放」和「展开」两个动作
        （前端源码：can_expand 为真时点行＝展开，否则＝执行动作），
        所以收藏只能做成展开后的动作条目。
        """
        station_id = station['contentId']
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f'{NODE_STATION_PREFIX}{station_id}',
            media_class=MediaClass.CHANNEL,
            media_content_type=MediaType.CHANNEL,
            title=station.get('title') or station_id,
            can_play=True,
            can_expand=True,
            can_search=True,
            thumbnail=station.get('image') or None,
            children=[
                self._action_node(ACTION_FAVORITE, station_id, '⭐ 收藏这个电台'),
                self._action_node(ACTION_UNFAVORITE, station_id, '取消收藏这个电台'),
            ],
        )

    def _action_node(self, action: str, station_id: str, title: str) -> BrowseMediaSource:
        """动作条目：点它就是执行动作，不推流（由媒体播放器实体拦截执行）。"""
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f'{NODE_ACTION_PREFIX}{action}/{station_id}',
            media_class=MediaClass.APP,
            media_content_type=MediaType.APP,
            title=title,
            can_play=True,
            can_expand=False,
        )
