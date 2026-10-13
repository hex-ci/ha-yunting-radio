# 云听电台 · Home Assistant 集成

**在 Home Assistant 的媒体浏览器里搜索、浏览、收藏云听的全国电台，一键推送到局域网音响播放。**

一个 Home Assistant 自定义集成（custom integration），把电台变成 HA 媒体库里可浏览、可搜索的内容源，并通过 DLNA/UPnP 把直播流推到音响上播放——顺带给出播放状态、音量、当前台名、设备电量。

**两类音响都能用：**

| 你的设备 | 能用到的功能 |
|---|---|
| **任意标准 DLNA/UPnP 音响**（智能音箱、电视、功放、网络播放器、路由器插件…） | 搜索电台、逐层浏览、收藏、最近播放、一键推送播放、播放控制与状态、音量；也可以**只启用内容源、不添加设备**，让你已有的播放器（官方 `dlna_dmr`、Music Assistant…）直接播 |
| **果壳智能音响（GEAK，型号 GM6）** | 以上全部，另加**电量传感器**和**重启 / 关闭音响**按钮；直播流地址前缀自动适配（这台设备要求带 `ffmpeg://`）。设备虽已停产，功能完整可用 |

> 不需要在 HA 里维护任何外部脚本或服务：装上集成即可用。

![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-41BDF5?logo=homeassistant&logoColor=white)
![License](https://img.shields.io/github/license/hex-ci/home-assistant-radio)

关键词：Home Assistant 集成 · 云听 · 中央广播电视总台 · 网络收音机 · 电台 · 媒体源 media_source · 媒体播放器 media_player · DLNA · UPnP · 智能音箱 · 音响 · 果壳智能音响 · GEAK · 智能家居

## 功能

| 功能 | 说明 |
|---|---|
| **搜索电台** | 媒体浏览器顶部搜索框，关键词（如「交通」「中国之声」）在**全国电台**里匹配，点结果即推送播放 |
| **浏览电台** | 按「央广频率」（19 个）或「按省份」（31 个省）逐层浏览 |
| **收藏** | 每个电台条目展开后有「⭐ 收藏这个电台」；也可以一键收藏当前正在播的台；收藏夹在媒体浏览器里直接点播 |
| **最近播放** | 播过的台自动记录（保留 20 条），不收藏也能找回 |
| **播放控制** | 播放 / 暂停 / 停止 / 上一台 / 下一台 / 音量 / 静音 |
| **设备状态** | 播放状态、音量、当前流地址、当前台名 |
| **电量** | `sensor.<设备>_battery`，标准 battery 设备类别，可直接放仪表盘电池卡片（设备需支持私有动作 `GetPowerStatus`，如**果壳智能音响**；其他设备不创建这个实体） |
| **重启 / 关闭音响** | 按钮点两次确认后才下发（防误触）；自动化里可直接用服务 `internet_radio.reboot` / `internet_radio.shutdown`。（设备需支持私有动作 `TimingShutdown`，如**果壳智能音响**） |
| **地址自愈** | 音响的 UPnP 端口会漂（实测 49152 / 49153 / 49154 之间变）、IP 也可能变；集成平时只用配置项里存的地址，连不上时按一下「重新发现设备」按钮（或发起播放）才发 SSDP 按 UDN 重发现，并把新地址回写配置项 |

## 安装

### HACS（推荐）

1. HACS → 集成 → 右上角菜单 → **自定义仓库**
2. 填 `https://github.com/hex-ci/home-assistant-radio`，类别选 **集成**
3. 搜索「云听电台」安装，然后**重启 Home Assistant**

### 手动

```bash
cd /path/to/homeassistant/config
git clone https://github.com/hex-ci/home-assistant-radio.git /tmp/home-assistant-radio
cp -r /tmp/home-assistant-radio/custom_components/internet_radio custom_components/
# 重启 Home Assistant
```

## 配置

**设置 → 设备与服务 → 添加集成 → 云听电台**，然后二选一：

| 选项 | 什么时候用 | 会发生什么 |
|---|---|---|
| **添加一台 DLNA 音响** | 想用本集成播放、看状态、看电量、重启/关机 | 集成自己发 SSDP 搜索列出局域网里的渲染器（果壳音响这类设备**不主动广播**，所以不走 HA 自带的发现缓存）；列表里没有你的设备时直接填 `http://<地址>:<端口>/renderer.xml` |
| **只启用云听电台内容源** | 已经有别的播放器（官方 `dlna_dmr`、Music Assistant…），只想要「云听电台」这个媒体库入口 | **不添加任何设备、不建任何实体**，只把云听电台注册进媒体库；播放时在媒体面板右下角把目标选成你自己的播放器即可（搜索是内容源自己的能力，不依赖播放器） |

只启用内容源时**没有任何设备是正常的**：媒体浏览器里照样能浏览电台、能搜索、能解析播放地址。

## 用法

| 想做什么 | 怎么做 |
|---|---|
| 搜索并播放 | 音响卡片 → **浏览媒体** → 云听电台 → 顶部搜索框输入关键词 → 点电台行尾的 **▶** |
| 按地区翻台 | 同上，进「央广频率」或「按省份」 |
| 收藏某个台 | 点电台那一行 → 展开 → **⭐ 收藏这个电台**（会弹通知确认） |
| 收藏当前在播的台 | 面板加按钮卡片 `button.<设备>_favorite_current_station` |
| 播放收藏的台 | 媒体浏览器 → 云听电台 → **我的收藏** → 点 ▶ |
| 自动化里推送 | 服务 `internet_radio.push_station`，`station: 中国之声`（台名模糊匹配或 contentId） |

> **为什么点电台那一行是展开、而不是直接播放**：Home Assistant 的媒体浏览器只支持「播放」和「展开」两个动作，没有给条目挂自定义按钮的机制（前端逻辑是 `can_expand ? 展开 : 执行动作`）。为了让每个台都能收藏，只能把电台做成可展开，代价就是播放要点行尾的 ▶。

> ⚠️ 在侧边栏「媒体」面板里点台之前，**右下角的播放目标要选成这台音响**（默认是「网页浏览器」，浏览器放不了这类流地址，会一直转圈）。从音响卡片的「浏览媒体」进去则不需要选。

## 实体与服务

**实体**

| 实体 | 说明 |
|---|---|
| `media_player.<设备>` | 播放控制 + 媒体浏览器入口 + 当前台名 |
| `sensor.<设备>_battery` | 电量（60 秒刷新） |
| `button.<设备>_favorite_current_station` | 收藏当前电台 |
| `button.<设备>_unfavorite_current_station` | 取消收藏当前电台 |
| `button.<设备>_rediscover_device` | 重新搜索设备地址（**平时不搜，按一下才搜**） |
| `button.<设备>_reboot_speaker` | 重启音响（**按两次确认**） |
| `button.<设备>_shutdown_speaker` | 关闭音响（**按两次确认**，断电后需手动开机） |

> 设备地址策略：**不做后台轮询搜索**。配置项里存着上次发现的地址，轮询只连这个地址（连不上就快速标不可用，不发 SSDP）。只有你按「重新发现设备」按钮、或发起播放时连不上，才会真的搜一次（6–8 秒），搜到就把新地址写回配置项。DLNA 音响常年关机，这样既不刷日志也不浪费。

> 重启 / 关机走设备私有动作 `TimingShutdown`，取值与官方 App 控制端一致（`0` 关机、`999` 重启）。官方端是「弹确认框 → 下发」，HA 的实体按钮弹不出模态框，所以做成「按两次确认」：第一次按只上膛并弹通知，30 秒内再按一次才真的下发（成功失败都会弹通知）。想换成单击 + 原生确认对话框，给卡片加 `confirmation` 即可。

**服务**

| 服务 | 说明 |
|---|---|
| `internet_radio.push_station` | 按台名或 contentId 推送；不指定实体就推给全部 |
| `internet_radio.add_favorite` / `internet_radio.remove_favorite` | 收藏 / 取消收藏；**不填台名就作用于当前在播的台** |
| `internet_radio.reboot` / `internet_radio.shutdown` | 直接下发重启 / 关机（**不经按钮的二次确认**，给自动化用） |
| `internet_radio.rediscover` | 按 UDN 重新搜索所有设备的地址并回写配置项，返回每台设备搜到的地址 |
| `internet_radio.clear_recent` | 清空「最近播放」 |

**选项**（设置 → 设备与服务 → 云听电台 → 配置）

| 选项 | 说明 |
|---|---|
| 加 `ffmpeg://` 前缀 | 添加设备时自动判断：果壳音响开、其他设备关。前缀是**播放设备**的特性，果壳音响要求直播流地址带它；其他 DLNA 设备收到裸 http 才能播 |
| 音质 | 高清优先（默认）或低码率优先 |
| 线路 | `auto` 按高清优先挑，也可固定 `m3u8` 或 `mp3` 线路 |

## 支持的设备

### 标准 DLNA/UPnP 音响（通用）

任何在局域网里以 `MediaRenderer` 应答的 UPnP/DLNA 设备都能加进来：智能音箱、电视、功放、网络播放器、路由器上的 DLNA 插件等等。

- 能用到：搜索、按央广/省份浏览、收藏、最近播放、推送播放、播放控制、状态与音量。
- 添加集成时**自动关掉** `ffmpeg://` 前缀（那只是果壳音响的要求）；设备放不了 m3u8 就在选项里把线路固定成 `mp3`。
- 它的私有动作（电量、重启/关机）都没有，所以那些实体不会创建。
- 已经有自己的播放器（官方 `dlna_dmr`、Music Assistant…）？可以**只启用内容源、不添加设备**，在媒体面板右下角把播放目标选成你的播放器即可。
- 搜索框不挑播放器：搜索由内容源自己负责（HA 的媒体浏览器浏览 `media-source://…` 节点时把搜索请求直接发给媒体源），所以任何播放目标（甚至「网页浏览器」）都能搜。

### 果壳智能音响（GEAK，型号 GM6）

在这台设备上做过真机实测（DIDL 宽容度、URI 前缀、私有动作、SSDP 应答率都有结论，见 [`docs/protocol-notes.md`](docs/protocol-notes.md)）。设备已停产，但功能完整：

- 上面**全部**功能都能用；
- 额外获得**电量传感器**（私有动作 `GetPowerStatus`）和**重启 / 关闭音响**按钮（私有动作 `TimingShutdown`，取值与官方 App 控制端一致：`0` 关机、`999` 重启）；
- 添加设备时**自动打开** `ffmpeg://` 前缀——这台设备要求直播流地址带前缀，裸 http 会被接受但不发声；
- 已知设备侧特性：不回报曲目元数据（台名由集成记）、不支持播放列表（多台连播请用自动化定时切台）。

## 常见问题

| 现象 | 处理 |
|---|---|
| 添加集成时列表里没有音响 | 设备不主动广播；手填 `http://<音响地址>:<端口>/renderer.xml` |
| 换了 IP 或端口 | 按一下「重新发现设备」按钮（或直接发起播放）：集成会按 UDN 搜一次并把新地址回写配置项 |
| 状态是 playing 但没声音（果壳音响） | 选项里的 `ffmpeg://` 前缀被关了，或直播地址里的时效令牌过期（重新点一次即可） |
| 普通 DLNA 设备推过去没声音 | 那条流（高清通常是 m3u8）设备放不了：在集成选项里把**线路**固定成 `mp3` 再试 |
| 点台一直转圈 | 侧边栏「媒体」面板的右下角播放目标还是「网页浏览器」，改成音响（或你自己的播放器） |
| 搜索没结果 | 搜索走本地索引（首次约 2 秒拉取）；网关限流或断网时会为空，稍后重试 |
| 看不到当前台名（果壳音响） | 设备不回报曲目元数据（把 `TrackMetaData` 回成字面量 `unkown`），台名由集成在推送时记下来 |

## 已知限制

- **果壳音响**不回报曲目元数据，所以台名是集成记的（HA 重启后第一条推送前为空）；直播流的播放进度也不可靠。普通 DLNA 设备如果能回报元数据，HA 侧会照常显示。
- **果壳音响**不支持播放列表：它不认 URI 里的列表文件（实测从不请求），所以只能一台一台点；多台连播请用 HA 自动化定时切台。
- 集成自带云听客户端（纯标准库），所有网络调用丢执行器跑，不阻塞事件循环。

## 目录

```
custom_components/internet_radio/    集成本体（自包含，含云听客户端）
  client.py                   云听接口客户端（纯标准库）
  media_player.py             媒体播放器实体（通用 DMR 客户端；播放时按设备选项加 ffmpeg:// 前缀）
  media_source.py             媒体源：浏览树 + 搜索 + 解析播放地址
  button.py  sensor.py        收藏按钮、电量传感器
  config_flow.py              添加设备（SSDP 发现或手填）/ 只启用内容源、选项
  discovery.py                自己发 SSDP 搜索 + 按 UDN 重发现
docs/                         协议实测结论、云听接口、集成细节
```

## 原理与协议

- 云听接口与签名规则：[`docs/radio-cn-api.md`](docs/radio-cn-api.md)
- DLNA 渲染器与果壳音响的实测协议事实：[`docs/protocol-notes.md`](docs/protocol-notes.md)
- 集成实现与排错细节：[`docs/home-assistant.md`](docs/home-assistant.md)

## English

**Yunting Radio — Home Assistant integration for 云听 internet radio.**

Browse, search and favorite Chinese radio stations from 云听 inside the Home Assistant media browser, then push the live stream to any DLNA/UPnP renderer on your LAN.

**Works with two kinds of speakers:**

| Your device | What you get |
|---|---|
| **Any standard DLNA/UPnP renderer** (smart speakers, TVs, receivers, network players, router plugins…) | Search, browse by station/province, favorites, recently played, play/volume/state; or turn on **media source only** and play it in your own player (`dlna_dmr`, Music Assistant…) |
| **GEAK (果壳) smart speaker, model GM6** | Everything above, plus the **battery sensor** and the **reboot / shutdown buttons**, with the `ffmpeg://` stream prefix handled automatically. Discontinued, but fully supported |

- Search box in the media browser (searching a media source goes straight to the source, so no player cooperation is needed — the bundled player exists for the device side: the `ffmpeg://` prefix and the device's private actions).
- Stations are playable items; each one expands into favorite / unfavorite actions.
- Entities: media player, battery sensor, favorite buttons, a re-discover button. Services: `push_station`, `add_favorite`, `remove_favorite`, `clear_recent`, `reboot`, `shutdown`, `rediscover`.
- Self-healing device address (UPnP ports drift): the address is searched **on demand only** — press the re-discover button, call `internet_radio.rediscover`, or simply hit play (a failed connect triggers one search) — and the new address is written back to the config entry. No background SSDP polling.

Install via HACS as a custom repository (category: integration) or copy `custom_components/internet_radio` into your config directory and restart. When adding the integration you can either add a DLNA speaker (full control: playback, state, battery, power buttons) or choose **media source only** — that registers the Yunting Radio source in your media library without creating any device, which is what you want if you already have a player (`dlna_dmr`, Music Assistant, …) and just need the stations.

## License

[MIT](LICENSE)
