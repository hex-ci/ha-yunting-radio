"""常量。"""
from __future__ import annotations

import logging

DOMAIN = 'yunting'
LOGGER = logging.getLogger(__package__)

STORAGE_VERSION = 1
STORAGE_KEY = f'{DOMAIN}.data'

# 配置项
CONF_URL = 'url'
CONF_UDN = 'udn'
# 选项：是否给直播流地址加前缀（果壳音响必须加）、音质与线路偏好
CONF_STREAM_PREFIX = 'stream_prefix'
CONF_QUALITY = 'quality'
CONF_LINE = 'line'

DEFAULT_STREAM_PREFIX = 'ffmpeg://'
DEFAULT_QUALITY = 'high'
DEFAULT_LINE = 'auto'

QUALITY_CHOICES = ['high', 'low']
LINE_CHOICES = ['auto', 'm3u8', 'mp3']

# 轮询间隔：设备支持 GENA 事件，但为了少踩端口变化的坑，第一版用轮询
SCAN_INTERVAL_SECONDS = 10

# 果壳智能音响的渲染器 UDN 前缀（用来在发现列表里优先展示）
GEAK_UDN_PREFIX = 'uuid:geakmusic'

# 服务
SERVICE_PUSH_STATION = 'push_station'
SERVICE_ADD_FAVORITE = 'add_favorite'
SERVICE_REMOVE_FAVORITE = 'remove_favorite'
SERVICE_CLEAR_RECENT = 'clear_recent'
SERVICE_REBOOT = 'reboot'
SERVICE_SHUTDOWN = 'shutdown'

ATTR_STATION = 'station'
ATTR_ENTITY_ID = 'entity_id'

# 最近播放保留条数
RECENT_LIMIT = 20

# UPnP 服务类型
AV_TRANSPORT_SERVICE = 'urn:schemas-upnp-org:service:AVTransport:1'
RENDERING_CONTROL_SERVICE = 'urn:schemas-upnp-org:service:RenderingControl:1'

# 私有动作 TimingShutdown 的 TimeOuts 取值（厂商文档口径：-1 取消、0 立即关机、999 重启）
TIMEOUT_CANCEL = -1
TIMEOUT_SHUTDOWN = 0
TIMEOUT_REBOOT = 999

# 媒体浏览器里的层级标识
NODE_ROOT = ''
NODE_NATIONAL = 'national'
NODE_PROVINCES = 'provinces'
NODE_PROVINCE_PREFIX = 'province/'
NODE_FAVORITES = 'favorites'
NODE_RECENT = 'recent'
NODE_STATION_PREFIX = 'station/'
# 树里留一个入口指向「所有媒体源」（本地媒体、TTS 等）
NODE_ALL_SOURCES = 'all_sources'
# 动作条目（点它就是执行动作，不推流）
NODE_ACTION_PREFIX = 'action/'
ACTION_FAVORITE = 'favorite'
ACTION_UNFAVORITE = 'unfavorite'
