"""云听（radio.cn）接口客户端。

复刻云听 App 的调用方式：网关 + 按接口分组密钥的 md5 签名。
列表接口只给低码率地址，高清地址要按 contentId 再取一次详情（`detail`）。

公开接口，密钥取自云听 App 客户端；网关会限流（短时间大批量请求返回 418），
所以全国电台列表带磁盘缓存，并发默认压到 6。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

GATEWAY = 'https://ytmsout.radio.cn'

# 密钥按接口路径分组（与云听 App 的密钥表一致）
GROUP_KEYS = {
    'format': '40359b279ed420d0643e12df721e6c4d',
    'contentBiz': '3196d2bb67a730342382ce8f690c8c44',
    'search': '68e251be8b49f462be367df22b212ee3',
    'series': '58325b785b752dd7d9c0281dd2befa6a',
    'publish': 'c1e49740f86d3126561c51131c4e384b',
    'live': '732abfe93639060ea6fc8a5f406d08a1',
    'news': '652e16dd26f38ac3d306b31e4839a8e3',
}

DEFAULT_CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')), 'yunting-radio')
DEFAULT_TIMEOUT = 15
MAX_WORKERS = 6


class YuntingError(RuntimeError):
    pass


class RateLimited(YuntingError):
    """网关限流（HTTP 418）。"""


def _group_of(path: str) -> str:
    for group in GROUP_KEYS:
        if f'/{group}/' in path:
            return group
    return 'news'


def _sign(path: str, params: dict, timestamp: str) -> str:
    """sign = md5(按 key 升序拼接的参数 + &timestamp=<毫秒>&key=<分组密钥>)。

    参数值参与签名时**不做 URL 编码**（与 URL 查询串的编码是两回事）。
    """
    raw = '&'.join(f'{k}={params[k]}' for k in sorted(params))
    if raw:
        raw += '&'
    raw += f'timestamp={timestamp}&key={GROUP_KEYS[_group_of(path)]}'
    return hashlib.md5(raw.encode()).hexdigest()


class YuntingClient:
    def __init__(self, cache_dir: str | None = None, cache_ttl: int = 86400,
                 timeout: int = DEFAULT_TIMEOUT, gateway: str = GATEWAY):
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.gateway = gateway.rstrip('/')

    # ---------------------------------------------------------------- 基础请求
    def get(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        params = dict(params or {})
        query = urllib.parse.urlencode(params) if params else ''
        url = self.gateway + path + (f'?{query}' if query else '')
        last = None
        for attempt in range(retries):
            ts = str(int(time.time() * 1000))
            req = urllib.request.Request(url, headers={
                'Content-Type': 'application/json',
                'equipmentId': '0000',
                'platformCode': 'ANDROID',
                'equipmentSource': 'ANDROID',
                'timestamp': ts,
                'sign': _sign(path, params, ts),
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as err:
                last = err
                if err.code == 418:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise YuntingError(f'HTTP {err.code} {path}') from err
            except Exception as err:  # 网络抖动
                last = err
                time.sleep(1 + attempt)
        raise YuntingError(f'请求失败：{path}（{last}）')

    # ---------------------------------------------------------------- 电台列表
    def provinces(self) -> list[dict]:
        data = self.get('/format/appProvince/list/all')
        return data.get('data') or []

    def national(self) -> list[dict]:
        """央广频率（国家台，19 个）。"""
        return self._normalize(self.get('/contentBiz/appBroadcast/all/group/1').get('data') or [], '央广')

    def by_province(self, province_code) -> list[dict]:
        data = self.get('/contentBiz/appBroadcast/allByProvince',
                        {'provinceCode': province_code}).get('data') or []
        return self._normalize(data, None)

    def all_stations(self, refresh: bool = False) -> list[dict]:
        """央广 + 31 个省，去重后约 941 个。带磁盘缓存（默认 1 天）。"""
        cache = os.path.join(self.cache_dir, 'stations.json')
        if not refresh and os.path.exists(cache):
            if time.time() - os.path.getmtime(cache) < self.cache_ttl:
                with open(cache, encoding='utf-8') as fp:
                    return json.load(fp)

        stations = {s['contentId']: s for s in self.national()}
        codes = [p['provinceCode'] for p in self.provinces()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for chunk in pool.map(self._safe_by_province, codes):
                for station in chunk:
                    stations.setdefault(station['contentId'], station)

        result = sorted(stations.values(), key=lambda s: s['contentId'])
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(cache, 'w', encoding='utf-8') as fp:
            json.dump(result, fp, ensure_ascii=False)
        return result

    def _safe_by_province(self, code):
        try:
            return self.by_province(code)
        except YuntingError:
            return []

    @staticmethod
    def _normalize(items: list[dict], tag: str | None) -> list[dict]:
        out = []
        for item in items:
            out.append({
                'contentId': str(item.get('contentId') or item.get('id') or ''),
                'title': item.get('title') or item.get('name') or '',
                'subtitle': item.get('subtitle') or '',
                'tagName': item.get('tagName') or tag or '',
                'image': item.get('image') or '',
                'playUrlLow': item.get('playUrlLow') or '',
                'mp3PlayUrlLow': item.get('mp3PlayUrlLow') or '',
            })
        return [s for s in out if s['contentId']]

    # ------------------------------------------------------------------ 详情
    def detail(self, content_id) -> dict:
        """电台详情：只有这里给高清地址（列表接口只给低码率）。"""
        data = self.get(f'/contentBiz/appBroadcast/detail/{content_id}').get('data')
        if not data:
            raise YuntingError(f'取不到电台详情：{content_id}')
        return data

    @staticmethod
    def pick_url(detail: dict, quality: str = 'high', line: str = 'auto') -> str:
        """按 高清优先、线路次之 挑播放地址。

        注意字段名不等于真实格式：`mp3PlayUrlHigh` 有时仍是 m3u8，看实际后缀。
        """
        high = ['playUrlHigh', 'mp3PlayUrlHigh']
        low = ['playUrlLow', 'mp3PlayUrlLow']
        if line == 'm3u8':
            order = ['playUrlHigh', 'playUrlLow', 'mp3PlayUrlHigh', 'mp3PlayUrlLow']
        elif line == 'mp3':
            order = ['mp3PlayUrlHigh', 'mp3PlayUrlLow', 'playUrlHigh', 'playUrlLow']
        else:
            order = (high + low) if quality == 'high' else (low + high)
        for field in order:
            value = detail.get(field)
            if value:
                return value
        raise YuntingError('该电台没有可用的播放地址')

    def fresh_url(self, content_id, **kwargs) -> str:
        """现取直链：地址里的 time 是时效令牌，必须每次现取。"""
        return self.pick_url(self.detail(content_id), **kwargs)

    # ------------------------------------------------------------------ 搜索
    def search(self, keyword: str, limit: int = 30, stations: list[dict] | None = None) -> list[dict]:
        """本地索引搜索：在央广 + 31 省的列表里按 台名/标签/副标题 匹配。

        云听的搜索接口没能试出关键词参数名（各种名字都返回空结果），所以走本地索引。
        """
        keyword = (keyword or '').strip()
        if not keyword:
            return []
        pool = stations if stations is not None else self.all_stations()
        words = keyword.split()
        hits = []
        for station in pool:
            haystack = f"{station['title']} {station['tagName']} {station['subtitle']}"
            if all(word in haystack for word in words):
                score = 0
                if station['title'] == keyword:
                    score += 100
                if keyword in station['title']:
                    score += 50
                if station['title'].startswith(keyword):
                    score += 20
                hits.append((score, station))
        hits.sort(key=lambda item: (-item[0], item[1]['contentId']))
        return [station for _, station in hits[:limit]]

    def resolve(self, name_or_id: str) -> dict:
        """把台名或 contentId 解析成一个电台条目。"""
        key = str(name_or_id).strip()
        if key.isdigit():
            for station in self.all_stations():
                if station['contentId'] == key:
                    return station
            return {'contentId': key, 'title': key, 'tagName': '', 'subtitle': '',
                    'image': '', 'playUrlLow': '', 'mp3PlayUrlLow': ''}
        hits = self.search(key, limit=1)
        if not hits:
            raise YuntingError(f'没有匹配「{key}」的电台')
        return hits[0]
