# 云听 App 接口

## 网关与签名

```
https://ytmsout.radio.cn          # 生产；备用 https://ytmsotherout.radio.cn
```

所有请求都带这几个 header：

| header | 值 |
| --- | --- |
| `Content-Type` | `application/json` |
| `equipmentId` | `0000`（服务端不校验，H5 用的是随机 UUID） |
| `platformCode` / `equipmentSource` | `ANDROID` |
| `timestamp` | 毫秒时间戳 |
| `sign` | 见下 |

```
sign = md5( 按 key 升序拼接的参数 + '&timestamp=' + ts + '&key=' + 分组密钥 )   # 小写 hex
```

没有参数时即 `md5('timestamp=' + ts + '&key=' + 密钥)`。

两个坑：

1. **参数值参与签名时不做 URL 编码**，URL 上的查询串要编码。纯数字参数看不出区别，中文参数会露馅：签名用 `searchWord=交通`，URL 用 `searchWord=%E4%BA%A4%E9%80%9A`。签名用了编码后的值会返回「参数验签错误」。
2. **带参数时网关先验签再路由**，所以「参数验签错误」不代表路径不存在；只有签名通过后的 404 才是路径不存在。

密钥按接口路径分组（与云听 App 的密钥表一致，见 `yunting/client.py` 的 `GROUP_KEYS`）。分组按路径特征判定：`/format/`、`/contentBiz/`、`/search/`、`/series/`、`/publish/`、`/live/`，其余归 `news`。

限流：短时间内大批量请求（几百次）会开始返回 **HTTP 418**，之后一段时间所有请求都 418。批量拉取要控速、设并发上限（本工具默认 6 并发 + 磁盘缓存 + 418 退避重试）。

## 接口

### 省份列表

```
GET /format/appProvince/list/all          # 无参数
→ {"code":0,"message":"SUCCESS","data":[{"provinceName":"安徽","provinceCode":340000}, ...]}
```

返回 31 个省级行政区，不含「国家/全部」项。

### 央广频率（国家台）

```
GET /contentBiz/appBroadcast/all/group/1  # 无参数
→ {"code":0,"data":[{"contentId":"639","title":"中国之声","subtitle":"正在直播：...",
    "image":"...","tagName":"中央","playUrlLow":"https://ytlive.radio.cn/130/radios/10639/index_10639.m3u8?...",
    "mp3PlayUrlLow":"...","listenCount":3098873076}, ...]}   # 19 个
```

`groupType` 只在 1（央广 19 个）、7（南海之声等 2 个）、9（大湾区/神州/台海等 7 个）有数据，其余返回空数组。

### 按省份的电台列表

```
GET /contentBiz/appBroadcast/allByProvince?provinceCode=110000
→ {"code":0,"data":[{"contentId":"357","title":"北京城市广播副中心之声","subtitle":"正在直播",
    "playUrlLow":"...","mp3PlayUrlLow":"...","tagName":"北京市台"}, ...]}
```

- `provinceCode` 省略时服务端默认返回北京，不是全国列表；`provinceCode=0` 返回 `data:null`。
- 省级列表**不含央广国家台**。全国电台 = 央广 19 个 + 31 个省各请求一次再合并，去重后约 **941 个**。
- 被忽略的参数（传了也没用）：`categoryId`、`categoryIds`、`tagName`、`contentType`、`pageIndex`、`pageSize`、`regionCode`。

### 电台详情（唯一给高清地址的接口）

```
GET /contentBiz/appBroadcast/detail/<contentId>
→ {"code":0,"data":{"id":"357","name":"北京城市广播副中心之声","image":"...",
    "playUrlLow":"...","playUrlHigh":"...","mp3PlayUrlLow":"...","mp3PlayUrlHigh":"...",
    "defaultHighFlag":0,"province":"北京市","isLiving":1}}
```

- 返回的是 `name`，列表里叫 `title`。
- 列表接口只给低码率，**要用高清就得按 contentId 补一次详情请求**。
- 地址里的 `time=` 是时效令牌，**必须现取现用**。

### 音质

| 字段 | 说明 |
| --- | --- |
| `playUrlLow` | 低码率 m3u8 |
| `playUrlHigh` | 高码率 m3u8 |
| `mp3PlayUrlLow` | mp3 线路低码率 |
| `mp3PlayUrlHigh` | mp3 线路高码率 |

**字段名不等于真实格式**：`mp3PlayUrlHigh` 有时是 `.mp3` 直链、有时仍是 `.m3u8`，按后缀判断，别按字段名。

### 搜索

App 的搜索路径是 `/search/search/findSearchResourceList`，但**没能试出关键词参数名**：试过 `keyword` / `keyWord` / `searchWord` / `searchKey` / `query` 等 40 多个名字，一律返回 `{"code":0,"data":{"totalNum":0,"data":[]}}`；POST 到该路径返回 503；拿 APK 字符串池穷举会被限流（418）。

所以本工具（与原始客户端一样）**用本地索引搜索**：把央广 + 31 省的 941 个电台拉一遍（6 并发，实测约 1.4 到 2 秒），在内存里按 `title` / `tagName` / `subtitle` 过滤。代价是搜索范围限于这 941 个电台。

### 节目单

```
GET /contentBiz/v1/appProgram/listByDate?date=2026-10-11&broadcastId=357
→ {"code":0,"data":[{"id":"978192013","programName":"健康投资家","startTime":1791669600000,
    "endTime":1791671400000,"playFlag":0}, ...]}   # 24 条
```

App 版**没有 `downloadUrl` / 播放地址字段**（Web 版才有），所以「推送昨日回放」这条路走不通。

### 未采用

`POST https://ytapi.radio.cn/ytsrv/srv/interactive/broadcastList` 是 H5「直播」tab 的电台列表，但它返回的是本文那套的**严格子集**（实测 170 个全部已包含在 941 个里，0 个独有），且只认 `categoryId`、没有省份维度。唯一多给的东西是 `playUrlMp3` 字段里有真 `.mp3` 直链，可作为兼容后备。
