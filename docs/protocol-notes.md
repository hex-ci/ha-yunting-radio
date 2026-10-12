# 协议与实测结论

本文件只记**可复现的事实**，来源分三类：`[实测]` 真机抓取/调用，`[客户端]` 从官方安卓客户端反编译或抓包得到，`[推测]` 尚未验证。

## 一、渲染器（果壳智能音响）

| 项 | 值 | 来源 |
|---|---|---|
| 设备类型 | `urn:schemas-upnp-org:device:MediaRenderer:1`（同一台还应答 MediaServer 与腾讯 QPlay） | [实测] |
| 渲染器 UDN | `uuid:geakmusic-<MAC>_MR` | [实测] |
| 端口 | **不固定**，随 UPnP 服务重启在 49152 / 49153 / 49154 之间变化；旧端口可能残留一个只回 HTTP 500 的实例 | [实测] |
| 服务 | AVTransport:1（57 个动作，含 41 个私有）、RenderingControl:1（6）、ConnectionManager:1（3） | [实测] |
| 事件 | AVTransport 的 `LastChange` 标注 `sendEvents=yes`，GENA 订阅可用（SUBSCRIBE 返回 200 + SID，超时 1800 秒） | [实测] |
| 广播 | 设备**不主动发 NOTIFY**，发现只能靠 M-SEARCH 应答；M-SEARCH 从临时端口发即可，**不要绑 1900**。而且**应答很挑：实测连续搜 5 次只回 2 次**（宿主机、容器里都一样），所以发现要在时间窗内每秒重发，单次发送会时灵时不灵 | [实测] |
| 设备描述 | 自报的 QPlay 服务 SCPD 命名空间非标准（`urn:schemas-tencent-com:service-1-0`），严格 XML 校验会直接拒绝；解析方需容忍 | [实测] |

### 推送播放

```
Stop → SetAVTransportURI(CurrentURI, CurrentURIMetaData) → Play(Speed=1)
```

- **直播流地址必须带 `ffmpeg://` 前缀**：`ffmpeg://http://<host>/.../index.m3u8?...`。传裸 `http://` 时设备照样回成功，但 `CurrentTransportState` 停在 `STOPPED`、不出声。[实测]
- 本地 mp3 文件用裸 `http://` 可以正常播放（原生播放器路径），直播流走 ffmpeg 管线所以需要前缀。[实测]
- 设备**不校验 DIDL**：`upnp:class` 用 `object.item.geakMusic`、`object.item.audioItem.musicTrack` 或 `object.item` 都照播；`protocolInfo` 用 `audio/mpeg`、`application/vnd.apple.mpegurl` 或 `application/octet-stream` 也都照播。[实测]
- DIDL 必须是合法 XML，否则设备回 SOAP Fault（XML error），推送静默失败。[实测]
- 播放模式位（`geakmusic://首曲|模式|列表URL|` 的第二段）：`1` 单曲循环、`2` 顺序、`3` 随机。[客户端]
- **播放列表机制不生效**：设备接受该 URI 形式并播放第一段音频，但**从不请求第三段的列表文件**（实测：直播流与本地 mp3 双曲列表各测一轮，HTTP 服务端 0 命中），播完第一首直接 STOPPED。多台连播必须在调用方做。[实测]

### 状态读取

| 动作 | 返回 |
|---|---|
| `GetTransportInfo` | `CurrentTransportState`：`PLAYING` / `STOPPED` / `PAUSED_PLAYBACK` / `TRANSITIONING` / `NO_MEDIA_PRESENT` |
| `RenderingControl.GetVolume` | `CurrentVolume` 0-100（需带 `Channel=Master`） |
| `GetMediaInfo` | `CurrentURI`；`CurrentURIMetaData` 恒为字面量 `unkown` |
| `GetPositionInfo` | `RelTime`；`TrackMetaData` 恒为 `unkown` |

- 设备**不回报曲目元数据**，所以控制端看不到台名，只能自己记。[实测]
- 直播流的 `RelTime` 不可靠（实测出现过 21 小时），不要拿它做进度显示。[实测]
- 只读 getter 不带 `InstanceID` 也被接受；写动作应显式带 `InstanceID=0`。[实测]

### 写动作的两个特性（影响客户端设计）

1. 部分写动作（改名、非法/畸形参数）会让 UPnP 服务**重启**：返回 200 后设备短暂不可达，约 10 到 13 秒后在**可能变化的端口**上自行恢复，不需要断电。
2. 因此**不要缓存端口**；缓存地址时要先取描述文件、再真调一次动作校验（只校验描述文件会误判，旧实例可能仍能取到描述文件但控制地址是死的）。

### 隐私

`GetWIFinfo` 明文返回当前 WiFi 密码，`GetAccountInfo` 明文返回绑定账号的邮箱/密码/token。**这两个接口的返回值不要写进日志、文档、截图或任何上报**。

## 二、接进 Home Assistant（2026.9 实测）

- 官方 `dlna_dmr` 集成能接入这台设备：它用 `UpnpFactory(non_strict=True)` 解析描述，正好容忍上面那个非标准 QPlay SCPD；GENA 可用，状态是推送式更新。
- 推送一条命令即可：

  ```yaml
  service: media_player.play_media
  data:
    entity_id: media_player.<设备>
    media_content_id: "ffmpeg://http://ytlive.radio.cn/131/radios/20639/index_20639.m3u8?type=1&key=...&time=..."
    media_content_type: music
  ```

  `media_content_id` 非 http 协议时 HA 的 `async_process_play_media_url` 原样透传，`ffmpeg://` 不会被改写；`media_content_type: music` 让 HA 生成的 DIDL 用 `object.item.audioItem.musicTrack`，实测被设备接受。
- HA 的媒体浏览器搜索框依赖播放器声明 `SEARCH_MEDIA`，而 `dlna_dmr` 只声明 `PLAY_MEDIA` 与 `BROWSE_MEDIA`，所以「在播放器里搜索」这条路不成立。[实测源码]
- 设备不回报元数据 → HA 面板能看到 播放/暂停/音量，看不到台名；台名要在推送方自己记。

## 三、云听（radio.cn）接口

见 [`radio-cn-api.md`](radio-cn-api.md)。
