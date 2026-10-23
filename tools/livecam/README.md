# livecam 慢直播云海对照采集

逐时抓取 5 路 B 站慢直播画面，同时拉取同机位坐标的 Open-Meteo 逐时预报，用仓库现有
`core.physics.sea_of_clouds.compute_sci` 计算云海指数，并把画面统计、预报和预测写入
同一行 JSONL 台账，供后续做"预测 vs 实际"校准。

## 用途

- 每个机位每小时抓取一张 JPEG 画面，作为"云/雾遮挡"的像素代理记录。
- 用机位自身的纬度、经度、海拔向 Open-Meteo 请求逐时地面要素和气压层廓线，匹配当前
  本地小时，再调用 `compute_sci` 得到 `sci`、`status`、`state` 等预测字段。
- 每路机位在同一小时产生一行 JSONL，画面与预测同处一行，便于按月汇总比对。
- `report.py` 只读台账，打印每个机位的样本数、帧成功率、预测状态分布和可疑组合。

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

# 覆盖当前小时已有记录
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

### 参数

| 参数 | 说明 |
| --- | --- |
| `--check` | 只做取流与单帧连通性检查，不写台账、不请求预报。 |
| `--only ROOM_ID` | 只处理指定 `room_id`，可重复使用；未找到的机位会提示。 |
| `--force` | 覆盖同一 `room_id` + `hour_local` 的已有记录（先剔除旧行再追加）。 |
| `--out DIR` | 输出根目录，默认 `data/livecam`。 |
| `--cams FILE` | 机位配置 JSON，默认本目录 `cams.json`。 |
| `--verbose` | 打印每路的帧统计与预测摘要。 |

不带 `--force` 时，若台账中已存在同一机位同一本地小时的记录，本轮会跳过该机位。

## 产物结构

```text
data/livecam/
├── frames/
│   ├── 1718159119/
│   │   └── 20261023T14.jpg
│   └── 23046729/
│       └── 20261023T14.jpg
└── ledger/
    └── 2026-10.jsonl
```

- `frames/<room_id>/<YYYYMMDDTHH>.jpg`：按本地小时命名的单帧画面。
- `ledger/YYYY-MM.jsonl`：按 `hour_local` 的年月分文件，每行一条 JSON 记录。

每条记录的主要字段：

| 字段 | 含义 |
| --- | --- |
| `ts_utc` | 本轮采集开始时间（UTC，`...Z`）。 |
| `ts_local` | 同一时刻的本地时间（Asia/Shanghai，带偏移）。 |
| `hour_local` | 归档所用的本地小时，`YYYY-MM-DDTHH:00`。 |
| `room_id` / `cam_name` | B 站房间号与机位名称。 |
| `coord_note` | 坐标精度备注，`exact` 或 `approximate`。 |
| `frame` | 帧文件路径、大小、MD5、尺寸及下述图像统计；失败时为 `null` 统计并带 `error`。 |
| `forecast_at` | Open-Meteo 实际匹配到的小时。 |
| `forecast` | 该小时的地面预报：温度、相对湿度、露点、低/中/高云量、能见度、风速、降水。 |
| `prediction` | `compute_sci` 结果：`sci`、`status`、`state`、`inversion_height_m`、`components`；预测失败或模型导入失败时为 `null`。 |
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

`frame` 还包含 `path`（相对输出根目录）、`ok`、`bytes`、`md5`、`width`、`height` 和
`error`（成功时为 `null`）。

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

d) 索松村海拔 3134m 已接近 700hPa 探空层顶（约 3000m），该机位探空信息稀薄，
   判据可靠性低于低海拔点位。

e) B 站直播流地址会过期，脚本每次运行重新调 `getRoomPlayInfo` 获取，不需要持久化 URL。
