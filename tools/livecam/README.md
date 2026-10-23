# livecam 慢直播云海对照采集

按 15 分钟时段（slot）抓取 5 路 B 站慢直播画面，同时拉取同机位坐标的 Open-Meteo
逐时预报，用仓库现有 `core.physics.sea_of_clouds.compute_sci` 计算云海指数，并把画面
统计、预报和预测写入同一行 JSONL 台账，供后续做"预测 vs 实际"校准。

## 用途

- 每个机位在同一 15 分钟时段内只抓取一张 JPEG 画面，作为"云/雾遮挡"的像素代理记录。
- 用机位自身的纬度、经度、海拔向 Open-Meteo 请求逐时地面要素和气压层廓线，匹配当前
  本地整点，再调用 `compute_sci` 得到 `sci`、`status`、`state` 等预测字段。
- 每路机位在同一时段产生一行 JSONL，画面与预测同处一行，便于按月汇总比对。
  同一小时内可以有多行（分别对应 00/15/30/45 分时段）。
- `mode` 表示采样节奏：整点 slot（分钟位 `00`，由逐时任务产生）为 `hourly`，
  15 分钟档 slot（分钟位 `15`/`30`/`45`，由加密任务产生）为 `dense`。
- `in_window` 表示该时刻是否落在日出日落窗口（05-08、17-20 点，含端点），与采样
  节奏相互独立：窗口内也可能有整点 `hourly` 记录，窗口外也可能有 15 分钟档 `dense` 记录。
- `report.py` 只读台账，打印每个机位的样本数、帧成功率、预测状态分布和可疑组合，
  并分别统计 `hourly` 与 `dense` 的样本数，以及窗口内/窗口外样本数。

## 运行方式

脚本默认可执行，依赖 `ffmpeg`、Pillow，以及仓库内的 `core/physics/sea_of_clouds.py`。
默认输出根目录为 `data/livecam`，默认机位配置为本目录的 `cams.json`。

```bash
# 连通性检查：只取流并解码一帧，不写台账
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  python3 tools/livecam/collect.py --check

# 只检查指定机位（--only 可重复）
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  python3 tools/livecam/collect.py --check --only 23046729 --only 24896254

# 单路实采
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  python3 tools/livecam/collect.py --only 23046729

# 全量实采，打印帧统计与预测细节
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  python3 tools/livecam/collect.py --verbose

# 覆盖当前 15 分钟时段已有记录
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  python3 tools/livecam/collect.py --force

# 自定义输出目录与机位配置
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  python3 tools/livecam/collect.py --out /tmp/livecam --cams tools/livecam/cams.json

# 汇总台账（只读）
python3 tools/livecam/report.py --out data/livecam
python3 tools/livecam/report.py --since 2026-10-01
```

必须绕开出口代理。脚本内部对 Open-Meteo / B 站请求使用无代理 opener，并在调用
`ffmpeg` 时清除代理环境变量；但运行命令仍建议显式使用
`env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy`，避免外层工具或
依赖代理的下载环节受影响。

### 定时任务建议

可以配置两条定时任务，共用同一个脚本，不需要额外的 job 编号或环境变量：

- 逐时任务：每小时的第 0 分钟运行一次，覆盖全天，产生 `hourly` 记录。
- 加密窗口任务：在 05-08、17-20 点内的 15/30/45 分运行，用于日出日落窗口加密采集，
  产生 `dense` 记录（整点仍由逐时任务负责）。

两条任务落在同一 slot 时会由台账去重自动跳过其中一个，不会重复抓帧。具体调度时间
可按机器负载调整，但应保证加密任务使用 15/30/45 分钟附近的触发时刻。

### 参数

| 参数 | 说明 |
| --- | --- |
| `--check` | 只做取流与单帧连通性检查，不写台账、不请求预报。 |
| `--only ROOM_ID` | 只处理指定 `room_id`，可重复使用；未找到的机位会提示。 |
| `--force` | 覆盖同一 `room_id` + `slot_local` 的已有记录（先剔除旧行再追加）。 |
| `--out DIR` | 输出根目录，默认 `data/livecam`。 |
| `--cams FILE` | 机位配置 JSON，默认本目录 `cams.json`。 |
| `--verbose` | 打印每路的帧统计与预测摘要。 |

不带 `--force` 时，若台账中已存在同一机位同一 15 分钟时段的记录，本轮会跳过该机位。
时段键 `slot_local` 是实际采集时刻向下取整到 15 分钟后的本地时间，例如 08:07 归入
`08:00`、05:23 归入 `05:15`。历史记录没有 `slot_local` 字段时，回退使用其
`hour_local` 作为时段键，因此旧台账仍会被识别为"已采过"，不会重复抓取。

## 产物结构

```text
data/livecam/
├── frames/
│   ├── 1718159119/
│   │   ├── 2026-10-23T0500.jpg
│   │   └── 2026-10-23T0515.jpg
│   └── 23046729/
│       └── 2026-10-23T0800.jpg
└── ledger/
    └── 2026-10.jsonl
```

- `frames/<room_id>/<YYYY-MM-DDTHHMM>.jpg`：按本地 slot 命名的单帧画面，分钟位只会是
  `00`、`15`、`30`、`45`。整点 slot 的文件名与历史逐时帧完全一致。
- `ledger/YYYY-MM.jsonl`：按 `hour_local` 的年月分文件，每行一条 JSON 记录；同一小时
  可以有多行不同 slot 的记录。

每条记录的主要字段：

| 字段 | 含义 |
| --- | --- |
| `ts_utc` | 本轮采集开始时间（UTC，`...Z`）。 |
| `ts_local` | 同一时刻的本地时间（Asia/Shanghai，带偏移）。 |
| `slot_local` | 去重与帧文件命名的本地 15 分钟时段，`YYYY-MM-DDTHH:MM`。 |
| `hour_local` | 归档与预报匹配所用的本地整点，`YYYY-MM-DDTHH:00`。 |
| `mode` | 采样节奏：整点 slot（分钟位 `00`）为 `hourly`，15 分钟档 slot（分钟位 `15`/`30`/`45`）为 `dense`。 |
| `in_window` | 该时刻是否处于日出日落窗口（05-08、17-20 点，含端点），布尔值。 |
| `room_id` / `cam_name` | B 站房间号与机位名称。 |
| `coord_note` | 坐标精度备注，`exact` 或 `approximate`。 |
| `frame` | 帧文件路径、大小、MD5、尺寸及下述图像统计；失败时为 `null` 统计并带 `error`。空帧同样保留真实统计，只是 `ok=false`、`blank=true`。 |
| `forecast_at` | Open-Meteo 实际匹配到的小时。 |
| `forecast` | 该小时的地面预报：温度、相对湿度、露点、低/中/高云量、能见度、风速、降水。 |
| `column_cloud` | 气柱云量汇总：`low`/`mid`/`high` 分别对应 `forecast` 的低/中/高云量，`max_cover` 为三者最大值（全为 `null` 时也是 `null`），`conflict` 为矛盾告警布尔值，`conflict_reason` 为告警说明或 `null`。详见下文。 |
| `prediction` | `compute_sci` 结果：`sci`、`status`、`state`、`inversion_height_m`、`components`；预测失败或模型导入失败时为 `null`。站点以上廓线层数不足时 `sci` 为 `null`、`status` 为 `"廓线不足"`、`state` 为 `"insufficient_profile"`，不参与云海判断。 |
| `errors` | 该路采集过程中的错误列表，成功时为空数组。 |

### frame 统计量

所有统计基于解码后的 JPEG 图像，灰度通道为 8 位（0–255）。

| 字段 | 定义 |
| --- | --- |
| `mean_luma` | 灰度均值。 |
| `p10_luma` / `p90_luma` | 灰度直方图的第 10 / 90 百分位。 |
| `std_luma` | 灰度标准差，反映整体明暗起伏。 |
| `dark_frac` | 灰度值小于 32 的像素占比。 |
| `bright_frac` | 灰度值大于等于 225 的像素占比。 |
| `edge_frac` | 与水平或垂直相邻像素灰度差超过 24 的像素占比（边缘/纹理密度代理）。 |
| `top_mean_luma` | 画面上方 40% 区域的平均灰度。 |
| `bottom_mean_luma` | 画面下方 40% 区域的平均灰度。 |
| `colorfulness` | 每个像素 RGB 三通道两两绝对差的均值的标准差，数值越大色彩越丰富。 |

`frame` 还包含 `path`（相对输出根目录）、`ok`、`blank`、`bytes`、`md5`、`width`、`height` 和
`error`（成功时为 `null`）。

### 气柱云量与矛盾告警

`column_cloud` 把预报中的低/中/高云量放到同一行，便于与画面实况对照：

| 子字段 | 含义 |
| --- | --- |
| `low` / `mid` / `high` | 分别为 `forecast.cloud_cover_low/mid/high`，缺失时为 `null`。 |
| `max_cover` | 三者中的最大值；三者全为 `null` 时为 `null`。 |
| `conflict` | 低云量与画面观感是否严重矛盾（仅低海拔机位适用）。 |
| `conflict_reason` | 矛盾时的说明，例如 `"低云量90%但画面明亮通透"`；否则为 `null`。 |
| `conflict_applicable` | 本次矛盾判定是否适用：观景点海拔为数值且低于 `LOW_CLOUD_BAND_TOP_M`（2000m）时为 `true`；海拔缺失或为 `null` 时为 `false`。 |
| `viewpoint_elevation_m` | 本次判定使用的观景点海拔（米）；传入值缺失或非数值时为 `null`。 |

当以下条件同时满足时 `conflict` 为 `true`：

- `conflict_applicable` 为 `true`（观景点低于低云带顶高 2000m；海拔 `>= 2000m` 或缺失时不适用）；
- `low >= 80.0`（模块常量 `LOW_CLOUD_CONFLICT_MIN`）；
- `frame.ok` 为 `true`（空帧/失败帧不参与判定）；
- `frame.mean_luma >= 100.0`（`CONFLICT_BRIGHT_LUMA_MIN`）；
- `frame.std_luma >= 50.0`（`CONFLICT_CONTRAST_STD_MIN`）。

含义是整柱低云覆盖率很高，画面却是明亮高对比的通透天空，二者互相矛盾。该矛盾只在
观景点低于 `LOW_CLOUD_BAND_TOP_M`（2000m）时成立：高海拔机位的低云带位于脚下，
"低云量高但画面明亮通透"实际是云海正信号，不判矛盾（该场景属云海判据范畴，本期不实现）。
三个阈值是初版阈值，待真实样本标定。`report.py` 的"云量矛盾"列按机位统计 `conflict`
为 `true` 的记录数。

### 廓线不足拒答

`_predict` 会先统计廓线中位于机位海拔及以上的层数（`height >= elev_m`，缺失或非数值
的层跳过）。当该层数少于 `MIN_LEVELS_ABOVE_VIEWPOINT`（当前为 2）时，不再调用
`compute_sci`，而是返回：

- `sci`: `null`
- `status`: `"廓线不足"`
- `state`: `"insufficient_profile"`
- `inversion_height_m`: `null`
- `components.note`: 说明站点以上实际可用层数

这是有意拒答：高海拔机位（如 3134m 的索松村）在现有气压层廓线中往往只有 1 层位于
地面以上，不足以判定云顶高度，不能给出自信的"晴空无云/5.0"错答案。此时该条记录不参与
云海判断，`report.py` 的状态分布会显示为"廓线不足"。`sci` 仅在 livecam 台账中允许为
`null`，`core/` 物理模块与 pipeline 的行为不受影响。

### 空帧/均匀帧质量闸门

抓帧成功不等于画面有效。采集器会用 `_image_stats` 算出的统计量做一次质量判定：

- `std_luma < 1.0`：整幅均匀，涵盖纯黑、纯白、纯灰等死流/占位画面。
- `mean_luma < 2.0`：近全黑画面。

命中任一条件时 `frame.ok` 为 `false`、`frame.blank` 为 `true`、`frame.error` 为
`"blank_frame"`，其余统计字段仍写入真实值，帧文件保留不删除，`errors` 中会追加
`空帧: <path>`，并且本轮该机位计入失败。两个阈值是 `collect.py` 中的模块级常量
`BLANK_STD_MIN` 与 `BLANK_MEAN_LUMA_MIN`。`report.py` 的帧成功率直接依据 `frame.ok`，
因此空帧会被正确计为失败样本；历史台账不做回溯修改。

## 退出码

- `0`：全部成功（或 `--check` 全部连通）。
- `2`：部分失败、配置读取失败、`--only` 无匹配机位，或 `--check` 存在不通机位。

部分机位失败不会中断整轮；但如果连续 3 个机位取流失败，脚本会提前结束并仍以 `2`
退出。台账写入失败的机位也按失败计。

## 已知局限

a) 帧统计只是"云/雾遮挡的像素代理"，不能等同于人工判读，也不构成云海标签。

b) 5 路机位全部位于浙江、西藏、四川、山东，均为外地样本，与河南预报点位无关。

c) `cams.json` 中 `coord_note` 为 `approximate` 的机位（1718159119 芳草地、
   22194248 国家地理）坐标误差可能达数公里，海拔为估算值，其 `prediction` 仅供参考。

d) 索松村海拔 3134m 已接近 700hPa 探空层顶（约 3000m），该机位探空信息稀薄。
   当站点以上层数不足时预测会明确返回"廓线不足"，而不是给出默认的晴空结论。

e) B 站直播流地址会过期，脚本每次运行重新调 `getRoomPlayInfo` 获取，不需要持久化 URL。
