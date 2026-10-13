"""配置流程：从 SSDP 发现里挑一台渲染器，或手填设备描述地址。"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import ssdp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import get_runtime
from .const import (
    CONF_LINE,
    CONF_QUALITY,
    CONF_STREAM_PREFIX,
    CONF_UDN,
    CONF_URL,
    DEFAULT_LINE,
    DEFAULT_QUALITY,
    DOMAIN,
    GEAK_UDN_PREFIX,
    LINE_CHOICES,
    QUALITY_CHOICES,
)
from .discovery import async_discover_renderers

MEDIA_RENDERER_ST = 'urn:schemas-upnp-org:device:MediaRenderer:1'


class YuntingConfigFlow(ConfigFlow, domain=DOMAIN):
    """添加设备。"""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """选设备或填地址。"""
        errors: dict[str, str] = {}
        devices = await self._async_discover()

        if user_input is not None:
            url = str(user_input[CONF_URL]).strip()
            info = await self._async_probe(url)
            if info is None:
                errors['base'] = 'cannot_connect'
            else:
                await self.async_set_unique_id(info['udn'])
                self._abort_if_unique_id_configured(updates={CONF_URL: url})
                return self.async_create_entry(
                    title=info['name'],
                    data={
                        CONF_URL: url,
                        CONF_UDN: info['udn'],
                        CONF_MANUFACTURER: info.get('manufacturer'),
                        CONF_MODEL: info.get('model'),
                    },
                    # 前缀是设备特性：果壳音响要求直播流带 ffmpeg://，其他 DLNA 设备默认不带
                    options={
                        CONF_STREAM_PREFIX: str(info['udn']).startswith(GEAK_UDN_PREFIX),
                    },
                )

        if devices:
            default = devices[0]['url']
            schema = vol.Schema({
                vol.Required(CONF_URL, default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=[{'value': d['url'], 'label': d['name']} for d in devices],
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            })
        else:
            schema = vol.Schema({vol.Required(CONF_URL, default='http://'): str})

        return self.async_show_form(step_id='user', data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ 工具
    async def _async_discover(self) -> list[dict]:
        """列出局域网里的渲染器，果壳音响排前面。

        用自己发的 SSDP 搜索（实时），不用 HA 的发现缓存 —— 有些设备（含果壳音响）
        不发 NOTIFY 广播，缓存里不会有它。再把 HA 缓存里的结果并进来兜底。
        """
        devices: dict[str, dict] = {}

        for info in await async_discover_renderers(self.hass):
            devices[info['url']] = {
                'url': info['url'],
                'name': f"{info['ip']}（{info['server'] or 'DLNA 渲染器'}）",
                'udn': info['udn'],
                'geak': GEAK_UDN_PREFIX in info['usn'].lower(),
            }

        try:
            cached = await ssdp.async_get_discovery_info_by_st(self.hass, MEDIA_RENDERER_ST)
        except Exception:  # noqa: BLE001
            cached = []
        for info in cached:
            if not info.ssdp_location or info.ssdp_location in devices:
                continue
            usn = (info.ssdp_usn or '').lower()
            name = (info.upnp.get('friendlyName') if info.upnp else None) \
                or info.ssdp_server or info.ssdp_location
            devices[info.ssdp_location] = {
                'url': info.ssdp_location,
                'name': f'{name}（{info.ssdp_location}）',
                'udn': info.ssdp_udn or usn.split('::')[0],
                'geak': GEAK_UDN_PREFIX in usn,
            }

        result = list(devices.values())
        result.sort(key=lambda d: (not d['geak'], d['name']))
        return result

    async def _async_probe(self, url: str) -> dict | None:
        """取设备描述，确认能连上并拿到 UDN、名字、厂商与型号。"""
        runtime = get_runtime(self.hass)
        try:
            device = await runtime.factory.async_create_device(url)
        except Exception:  # noqa: BLE001
            return None
        return {
            'udn': device.udn,
            'name': getattr(device, 'friendly_name', None) or device.udn,
            'manufacturer': getattr(device, 'manufacturer', None),
            'model': getattr(device, 'model_name', None),
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """选项流程。"""
        return YuntingOptionsFlow()


class YuntingOptionsFlow(OptionsFlow):
    """播放选项：是否加流前缀、音质与线路。"""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title='', data=user_input)

        options = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(
                CONF_STREAM_PREFIX,
                default=options.get(CONF_STREAM_PREFIX, True),
            ): bool,
            vol.Optional(
                CONF_QUALITY,
                default=options.get(CONF_QUALITY, DEFAULT_QUALITY),
            ): vol.In(QUALITY_CHOICES),
            vol.Optional(
                CONF_LINE,
                default=options.get(CONF_LINE, DEFAULT_LINE),
            ): vol.In(LINE_CHOICES),
        })
        return self.async_show_form(step_id='init', data_schema=schema)
