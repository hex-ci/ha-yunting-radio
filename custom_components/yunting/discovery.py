"""自己发 SSDP M-SEARCH 找渲染器。

为什么不直接用 `homeassistant.components.ssdp`：它返回的是**缓存**，而果壳音响这类设备
不发 NOTIFY 广播（只应答搜索），缓存里往往根本没有它 —— 实测下拉列表里就没有。
所以这里自己发搜索报文，拿到实时结果；同时提供按 UDN 精确找地址的能力，
用来在设备端口漂移（49152 / 49153 / 49154 之间变）之后把地址找回来。
"""
from __future__ import annotations

import re
import socket
import time

from homeassistant.core import HomeAssistant

from .const import LOGGER

SSDP_ADDRESS = '239.255.255.250'
SSDP_PORT = 1900
MEDIA_RENDERER_ST = 'urn:schemas-upnp-org:device:MediaRenderer:1'

# 设备不会主动广播，只能靠 M-SEARCH；从临时端口发即可，绑 1900 反而收不到应答
# 应答很不稳定（实测 5 次只回 2 次），所以时间窗内每秒重发一次
DEFAULT_TIMEOUT = 8.0
DEFAULT_ROUNDS = 1


def _m_search(timeout: float, rounds: int, st: str) -> list[dict]:
    """阻塞式搜索（丢执行器里跑）。返回 [{location, url, udn, usn, ip, server}]。

    这台设备的 SSDP 应答很挑：实测连续搜 5 次只回 2 次（宿主机、容器里都一样），
    所以不能只发一轮 —— 在时间窗内每秒重发一次，命中率才够（6 秒窗口 ≈ 95%）。
    """
    message = (
        'M-SEARCH * HTTP/1.1\r\n'
        f'HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n'
        'MAN: "ssdp:discover"\r\n'
        f'MX: {max(1, int(timeout / 2))}\r\n'
        f'ST: {st}\r\n\r\n'
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    try:
        found: dict[str, dict] = {}
        deadline = time.time() + timeout
        next_send = 0.0
        while time.time() < deadline:
            now = time.time()
            if now >= next_send:
                for _ in range(max(1, rounds)):
                    sock.sendto(message, (SSDP_ADDRESS, SSDP_PORT))
                next_send = now + 1.0

            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            text = data.decode(errors='ignore')
            location = re.search(r'LOCATION:\s*(\S+)', text, re.I)
            if not location:
                continue
            usn = re.search(r'USN:\s*(\S+)', text, re.I)
            server = re.search(r'SERVER:\s*([^\r\n]+)', text, re.I)
            usn_value = usn.group(1) if usn else ''
            found[location.group(1)] = {
                'location': location.group(1),
                'url': location.group(1),
                # USN 形如 uuid:geakmusic-xxx_MR::urn:schemas-upnp-org:device:MediaRenderer:1
                'udn': usn_value.split('::')[0],
                'usn': usn_value,
                'ip': addr[0],
                'server': server.group(1).strip() if server else '',
            }
        return list(found.values())
    finally:
        sock.close()


async def async_discover_renderers(hass: HomeAssistant, timeout: float = DEFAULT_TIMEOUT,
                                   rounds: int = DEFAULT_ROUNDS,
                                   st: str = MEDIA_RENDERER_ST) -> list[dict]:
    """搜局域网里的渲染器（实时结果，不是 HA 的缓存）。"""
    try:
        return await hass.async_add_executor_job(_m_search, timeout, rounds, st)
    except Exception as err:  # noqa: BLE001
        LOGGER.debug('SSDP 搜索失败：%s', err)
        return []


async def async_find_location_by_udn(hass: HomeAssistant, udn: str,
                                     timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """按 UDN 找设备当前的描述文件地址（端口变了也能找回来）。"""
    if not udn:
        return None
    target = udn.strip().lower()
    for info in await async_discover_renderers(hass, timeout=timeout):
        if info['udn'].lower() == target:
            return info['location']
    LOGGER.debug('按 UDN 没搜到设备：%s', udn)
    return None
