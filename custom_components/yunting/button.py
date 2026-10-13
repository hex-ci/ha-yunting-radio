"""按钮实体：收藏 / 取消收藏当前电台、重新发现设备、重启设备、关闭设备。

HA 的媒体浏览器是只读浏览，不支持「对某个条目执行动作」，所以这类动作都做成按钮，
放进仪表盘点一下就行。当前电台的判定见 `__init__.async_current_station`。
"""
from __future__ import annotations

import time

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import async_current_station, get_runtime
from .const import (
    AV_TRANSPORT_SERVICE,
    CONF_UDN,
    CONF_URL,
    DOMAIN,
    LOGGER,
    TIMEOUT_REBOOT,
    TIMEOUT_SHUTDOWN,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """为每台设备加按钮；重启/关机只在这台设备支持时才建。"""
    udn = entry.data[CONF_UDN]
    entities: list[ButtonEntity] = [
        YuntingFavoriteButton(hass, entry, udn, add=True),
        YuntingFavoriteButton(hass, entry, udn, add=False),
        YuntingRediscoverButton(hass, entry, udn),
    ]
    if await _async_supports_power(hass, entry):
        entities += [
            YuntingPowerButton(hass, entry, udn, reboot=True),
            YuntingPowerButton(hass, entry, udn, reboot=False),
        ]
    else:
        LOGGER.debug('设备不支持 TimingShutdown，不创建重启/关机按钮')
    async_add_entities(entities)


async def _async_supports_power(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """设备是否支持私有的 TimingShutdown（果壳音响有，标准 DLNA 设备没有）。

    直接连配置项里存的地址，**不做 SSDP 重发现**：探测只是为了让实体清单正确，
    不值得为它等一次 6–8 秒的搜索（设备离线时会拖慢平台装载）。
    连不上时按「支持」处理：不要因为设备临时离线就把功能藏起来。
    """
    runtime = get_runtime(hass)
    try:
        device = await runtime.factory.async_create_device(entry.data[CONF_URL])
        service = device.service(AV_TRANSPORT_SERVICE)
        return service.action('TimingShutdown') is not None
    except Exception as err:  # noqa: BLE001
        LOGGER.debug('探测 TimingShutdown 失败（按支持处理）：%s', err)
        return True


class YuntingButton(ButtonEntity):
    """按钮基类。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, udn: str,
                 key: str) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f'{udn}_{key}'
        self._attr_translation_key = key
        self._attr_device_info = {'identifiers': {(DOMAIN, udn)}}

    def _notify(self, message: str) -> None:
        """给一条反馈（同一个 id 覆盖，不会堆）。"""
        persistent_notification.async_create(
            self.hass, message, title='云听电台',
            notification_id=f'{DOMAIN}_button')


class YuntingFavoriteButton(YuntingButton):
    """收藏 / 取消收藏当前电台。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, udn: str,
                 add: bool) -> None:
        super().__init__(hass, entry, udn, 'favorite_current' if add else 'unfavorite_current')
        # unique_id 保持历史取值，避免升级后 HA 当成新实体、留下重复实体
        self._attr_unique_id = f'{udn}_favorite' if add else f'{udn}_unfavorite'
        self._add = add

    async def async_press(self) -> None:
        """按一下：收藏 / 取消收藏当前电台。"""
        runtime = get_runtime(self.hass)
        station = await async_current_station(self.hass)
        if station is None:
            LOGGER.warning('认不出当前正在播放的电台，请用 yunting.add_favorite 指定台名')
            return
        if self._add:
            added = await runtime.async_add_favorite(station)
            message = (f'已收藏「{station["title"]}」' if added
                       else f'「{station["title"]}」已经在收藏里')
        else:
            removed = await runtime.async_remove_favorite(station['contentId'])
            message = (f'已取消收藏「{station["title"]}」' if removed
                       else f'「{station["title"]}」本来就不在收藏里')
        LOGGER.info('%s', message)
        self._notify(message)


class YuntingPowerButton(YuntingButton):
    """重启 / 关闭设备（**需要按两次确认**）。

    用的是设备的私有动作 `TimingShutdown`，`TimeOuts` 取值与官方 App 控制端
    （geak-audio-app 的 `App.vue` / `device.js`）一致：`0` 立即关机、`999` 重启，
    那边是「弹确认框 → 下发」，HA 的实体按钮弹不出模态框，所以这里用
    「按两次确认」等价实现。

    第一次按只是「上膛」并弹通知提示再按一次，`ARM_SECONDS` 秒内第二次按才真的下发；
    超时或换个按钮都得重新按两次。自动化里要直接执行（不要确认）用服务
    `yunting.reboot` / `yunting.shutdown`。
    """

    _attr_device_class = ButtonDeviceClass.RESTART

    ARM_SECONDS = 30

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, udn: str,
                 reboot: bool) -> None:
        super().__init__(hass, entry, udn, 'reboot' if reboot else 'shutdown')
        self._reboot = reboot
        self._label = '重启音响' if reboot else '关闭音响'
        self._armed_at: float | None = None
        if not reboot:
            # 关机没有对应的标准设备类别
            self._attr_device_class = None

    @property
    def extra_state_attributes(self) -> dict:
        return {'armed': self._armed(), 'label': self._label}

    def _armed(self) -> bool:
        return (self._armed_at is not None
                and time.monotonic() - self._armed_at <= self.ARM_SECONDS)

    async def async_press(self) -> None:
        """第一次按＝上膛，第二次按＝执行。"""
        if not self._armed():
            self._armed_at = time.monotonic()
            LOGGER.info('「%s」已上膛，%s 秒内再按一次才生效', self._label, self.ARM_SECONDS)
            self._notify(f'确认要{self._label}吗？请在 {self.ARM_SECONDS} 秒内再按一次「{self._label}」。')
            self.async_write_ha_state()
            return

        self._armed_at = None
        self.async_write_ha_state()

        runtime = get_runtime(self.hass)
        timeout = TIMEOUT_REBOOT if self._reboot else TIMEOUT_SHUTDOWN
        try:
            await runtime.async_call_action(
                self.entry, 'TimingShutdown',
                service=AV_TRANSPORT_SERVICE, InstanceID=0, TimeOuts=timeout)
        except Exception as err:  # noqa: BLE001
            LOGGER.warning('下发「%s」失败：%s', self._label, err)
            self._notify(f'{self._label}失败：设备可能已离线（{err}）')
            raise
        message = ('已发送重启指令：设备约 30 秒后回来，集成会自动重连'
                   if self._reboot else '已发送关机指令：设备会断电，需要手动开机')
        LOGGER.info('%s', message)
        self._notify(message)


class YuntingRediscoverButton(YuntingButton):
    """重新搜索设备地址（按一下才搜，平时不搜）。

    SSDP 搜索要 6–8 秒，而这台音响（以及大多数 DLNA 音响）常年关机，所以集成**不做**
    后台轮询搜索：配置项里存着上次发现的地址，直接用；只有你按这个按钮、或者发起播放
    时连不上，才会真的去搜一次。搜索到了就把新地址回写到配置项。
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, udn: str) -> None:
        super().__init__(hass, entry, udn, 'rediscover')

    async def async_press(self) -> None:
        """搜一次并回写地址。"""
        runtime = get_runtime(self.hass)
        old_url = str(self.entry.data.get(CONF_URL) or '')
        new_url = await runtime.async_rediscover(self.entry, force=True)
        if not new_url:
            LOGGER.info('重新发现：没搜到「%s」（设备可能没开机）', self.entry.title)
            self._notify(f'没搜到「{self.entry.title}」：设备可能没开机，或者不在同一个网段。')
            return
        if new_url == old_url:
            LOGGER.info('重新发现：地址没变（%s）', new_url)
            self._notify(f'「{self.entry.title}」地址没变：{new_url}')
            return
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_URL: new_url})
        LOGGER.info('重新发现：地址已更新 %s → %s', old_url, new_url)
        self._notify(f'「{self.entry.title}」地址已更新：{old_url} → {new_url}')
