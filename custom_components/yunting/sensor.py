"""传感器：设备电量（AVTransport 的 GetPowerStatus，果壳音响私有动作）。

电量变化慢，60 秒轮询一次就够。设备地址变了同样会自动重发现（与媒体播放器同一套逻辑）。
"""
from __future__ import annotations

from datetime import timedelta

from async_upnp_client.client import UpnpDevice

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import get_runtime
from .const import CONF_UDN, CONF_URL, DOMAIN, LOGGER

SCAN_INTERVAL = timedelta(seconds=60)
PARALLEL_UPDATES = 1

AV_TRANSPORT = 'urn:schemas-upnp-org:service:AVTransport:1'


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """建电量实体；设备是否支持 GetPowerStatus 在第一次轮询时判断。

    不在启动时探测：设备离线时探测要等连接超时 + 重发现（10 秒以上），会拖慢平台
    装载，而且离线时反而会把实体藏起来。设备不支持时实体在第一次轮询后自己移除。
    """
    async_add_entities([YuntingBatterySensor(hass, entry)])


class YuntingBatterySensor(SensorEntity):
    """电量。"""

    _attr_has_entity_name = True
    _attr_translation_key = 'battery'
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._udn: str = entry.data[CONF_UDN]
        self._device: UpnpDevice | None = None
        # None = 还没探到，True/False = 设备支持/不支持这个动作
        self.supported: bool | None = None
        self._attr_unique_id = f'{self._udn}_battery'
        self._attr_device_info = {'identifiers': {(DOMAIN, self._udn)}}

    # ---------------------------------------------------------------- 连接
    async def _async_connect(self, url: str) -> bool:
        runtime = get_runtime(self.hass)
        try:
            self._device = await runtime.factory.async_create_device(url)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug('电量传感器连不上 %s：%s', url, err)
            self._device = None
            return False
        return True

    async def _async_rediscover(self) -> bool:
        """连不上就按 UDN 重新发现，并把新地址回写配置项（带冷却，避免离线时反复搜）。"""
        url = self.entry.data.get(CONF_URL)
        new_url = await get_runtime(self.hass).async_rediscover(self.entry)
        if not new_url or new_url == url or not await self._async_connect(new_url):
            return False
        LOGGER.info('电量传感器：设备地址已变化：%s → %s', url, new_url)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_URL: new_url})
        return True

    # ---------------------------------------------------------------- 轮询
    async def async_update(self) -> None:
        """读电量；第一次连上时判断设备是否支持这个动作。"""
        if self._device is None and not await self._async_connect(self.entry.data[CONF_URL]):
            if not await self._async_rediscover():
                self._attr_available = False
                return
        assert self._device is not None
        try:
            service = self._device.service(AV_TRANSPORT)
            action = service.action('GetPowerStatus')
            if action is None:
                # 设备没有这个私有动作（非果壳设备）：实体自己消失，别留个永远空的实体
                LOGGER.debug('设备不支持 GetPowerStatus，电量实体自行移除')
                self.supported = False
                await self.async_remove()
                return
            result = await action.async_call(InstanceID=0)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug('读电量失败：%s', err)
            self._device = None
            self._attr_available = False
            return

        power = result.get('PowerStatus')
        try:
            self._attr_native_value = int(str(power).strip())
        except (TypeError, ValueError):
            LOGGER.debug('电量返回值认不出：%r', power)
            return
        self._attr_available = True
        self.supported = True
