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
from .discovery import async_find_location_by_udn

SCAN_INTERVAL = timedelta(seconds=60)
PARALLEL_UPDATES = 1

AV_TRANSPORT = 'urn:schemas-upnp-org:service:AVTransport:1'


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """先探一次，确认设备支持 GetPowerStatus 再建实体。"""
    sensor = YuntingBatterySensor(hass, entry)
    await sensor.async_probe()
    if sensor.supported is False:
        LOGGER.debug('设备不支持 GetPowerStatus，不创建电量实体')
        return
    async_add_entities([sensor])


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
        """连不上就按 UDN 重新发现，并把新地址回写配置项。"""
        url = self.entry.data.get(CONF_URL)
        new_url = await async_find_location_by_udn(self.hass, self._udn)
        if not new_url or new_url == url or not await self._async_connect(new_url):
            return False
        LOGGER.info('电量传感器：设备地址已变化：%s → %s', url, new_url)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_URL: new_url})
        return True

    # ---------------------------------------------------------------- 轮询
    async def async_probe(self) -> None:
        """建实体前探一次：能不能连、有没有 GetPowerStatus。"""
        if not await self._async_connect(self.entry.data[CONF_URL]):
            await self._async_rediscover()
        if self._device is None:
            return
        try:
            service = self._device.service(AV_TRANSPORT)
            self.supported = service.action('GetPowerStatus') is not None
        except Exception:  # noqa: BLE001
            self.supported = False

    async def async_update(self) -> None:
        """读电量。"""
        if self._device is None and not await self._async_connect(self.entry.data[CONF_URL]):
            if not await self._async_rediscover():
                self._attr_available = False
                return
        assert self._device is not None
        try:
            service = self._device.service(AV_TRANSPORT)
            action = service.action('GetPowerStatus')
            if action is None:
                self.supported = False
                self._attr_available = False
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
