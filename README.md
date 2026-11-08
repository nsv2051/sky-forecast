# Sky Forecast · 河南中西部天空观景指数

纯 Python 标准库实现的火烧云 / 云海逐时预报系统，为河南中西部伏牛山脉及周边 11 个机位生成 96 小时逐时数据，并以零构建单页发布在 GitHub Pages。

| 入口 | 位置 |
| --- | --- |
| 线上站点 | https://nsv2051.github.io/sky-forecast/ |
| 仓库 | https://github.com/nsv2051/sky-forecast |
| 前端 | `public/index.html`（单文件，CSS/JS 全内联） |
| 数据 | `public/data/forecast.json`（构建产物，不入库） |
| 自动化 | `.github/workflows/forecast.yml`，每 2 小时自动生成并发布 |

---

## 1. 快速开始

要求 Python ≥ 3.9，无第三方依赖，也不需要 `requirements.txt`：

```bash
python3 scripts/generate_forecast.py --no-grid   # 生成 public/data/forecast.json
python3 scripts/serve.py                         # 打开 http://127.0.0.1:8080/
python3 -m unittest discover -s tests -q         # 84 个测试，全部通过
```

首次只想离线看效果，可把第一条换成 `python3 scripts/generate_forecast.py --mock --no-grid`。

---

## 2. 功能

页面零构建、CSS/JS 全内联、无外部依赖与 CDN。数据只从同源相对路径 `data/forecast.json` 读取，刷新时附加 bust 参数；页脚另链到 `api/forecast`。

| 板块 | 标题元素 | 内容 |
| --- | --- | --- |
| 黄金窗口看点 | `#bannerTitle` | 未来 3 天最佳日出 / 日落窗口，按当日 BCI 峰值排序，最优当日标为黄金窗口 |
| 单点专业剖析 | `#detailTitle` | 选定机位与时刻的完整剖面：太阳高度角、画布质量、进光廊道、低云遮挡、能见度、BCI / SCI 分量与决策建议 |
| 机位总览 | `#overviewTitle` | 11 个机位 × 未来 3 天的 BCI / SCI 对照 |

主要交互元素：

| 元素 | id | 作用 |
| --- | --- | --- |
| 机位下拉 | `#spotSelect` | 切换剖析机位 |
| 时刻下拉 | `#timeSelect` | 切换剖析时刻 |
| 刷新按钮 | `#refreshBtn` | 重新拉取同源 JSON |
| 深色切换 | `#themeBtn` | 切换深色 / 浅色主题 |
| 数据源标签 | `#srcChips` | 展示 ECMWF / QWeather / 降级来源 |
| 页脚更新时间 | `#footUpdated` | 显示 `meta.generated_at` |

页脚「实时 API」链接指向 `api/forecast`。GitHub Pages 没有服务端，该链接在线上会返回 404；需要实时重算时使用本地 API 或可选的 Vercel 部署。

---

## 3. 机位清单

`config/spots.json` 共 11 个机位，海拔单位为 m。`preferences` 记录机位能力：`sea_of_clouds` 决定是否参与云海评分，`burning_cloud` 决定是否参与火烧云评分，`priority` 为人工标注的优先级，当前未被代码消费。

| id | 名称 | 海拔 | 云海 | priority |
| --- | --- | ---: | :---: | --- |
| `zhengzhou_urban` | 郑州市区 | 109 | false | high |
| `nanyang_urban` | 南阳市区 | 126 | false | medium |
| `songshan_junjifeng` | 嵩山·峻极峰 | 1492 | true | high |
| `songshan_yunding` | 嵩山·云顶草甸 | 1345 | true | high |
| `wuzhiling` | 五指岭 | 1121 | true | high |
| `laojunshan_jinding` | 老君山·金顶 | 2217 | true | high |
| `baiyunshan_yuhuangding` | 白云山·玉皇顶 | 2216 | true | high |
| `laojieling_jijiaojian` | 老界岭·犄角尖 | 2223 | true | high |
| `tongbaishan_taibaiding` | 桐柏山·太白顶 | 1140 | true | medium |
| `wuduoshan` | 五朵山 | 1626 | true | medium |
| `baotianman` | 宝天曼 | 1803 | true | medium |

平原市区海拔低、始终位于云顶之下，`sea_of_clouds=false`，不参与云海评分。

---

## 4. 物理模型

全部模块使用纯 Python 标准库，并以 `from __future__ import annotations` 保持前向兼容。核心逻辑位于 `core/physics/` 与 `core/pipeline.py`。

### 4.1 太阳几何 `core/physics/solar.py`

采用 NOAA 太阳位置算法，导出：

- `solar_position`：太阳高度角、方位角、时角
- `sunrise_sunset`：日出 / 日落时刻
- `golden_hour_windows`：黄金光窗口
- `solar_noon`：太阳正午

前后端共享同一套时间口径，页面上的太阳数据与 JSON 逐时字段一致。

### 4.2 火烧云指数 BCI `core/physics/burning_cloud.py`

0–100 分，三个分量按加权几何平均合成：

| 分量 | 权重 | 含义 |
| --- | ---: | --- |
| 云量画布 | 0.55 | 中云 / 高云的透光与承接掠射光能力 |
| 能见度 | 0.25 | 地面水平能见度 |
| 气溶胶 | 0.20 | AQI，仅在有真实观测时施加 |

关键在于乘性物理闸门：太阳落得过深或光路被低云带封死时，无论画布多好都会被打回低分。相关常量：

| 常量 | 值 | 含义 |
| --- | ---: | --- |
| `LOW_CLOUD_CLEAR` | 35.0 | 低云低于该百分比时视为通透 |
| `LOW_CLOUD_SEALED` | 75.0 | 低云高于该百分比时视为封死 |
| `TRANSLUCENT_HIGH` | 75.0 | 理想半透明高云带上限 |
| `_CANVAS_MID_ONLY_FLOOR` | 0.55 | 只有中云、没有高云承接时的画布折减下限 |

BCI 分档由 `grade_bci` 给出：`≥75 极佳`、`≥55 良好`、`≥35 一般`、`<35 较差`。

### 4.3 云海指数 SCI `core/physics/sea_of_clouds.py`

分析各气压层的温度、湿度与位势高度，定位逆温层与凝结高度，再与机位海拔比较，输出四种状态：

| 常量 | 状态 | 含义 |
| --- | --- | --- |
| `STATUS_OVERLOOK` | 俯瞰云海 | 机位在云顶之上，向下看云 |
| `STATUS_IN_CLOUD` | 云中雾中 | 机位位于云层内部 |
| `STATUS_BELOW` | 云层在上方 | 云层压在头顶 |
| `STATUS_CLEAR` | 晴空无云 | 无组织云层或无明显逆温 |

公开函数包括 `condensation_level`、`detect_inversion`、`compute_sci`、`sea_potential`。

### 4.4 预计色彩 `core/physics/spectrum.py`

纯标准库实现日出 / 日落直射光在 400–700 nm（每 10 nm）的透射谱，并转换为 sRGB 颜色，用于在单点剖析中展示"预计霞光色彩"。霞光照亮的是**云底**，所以光深必须从云层高度起算，而不是一路算到地面。物理链路如下：

1. **光深起点在云层高度**：高云 8600 m、中云 5200 m、低云 1500 m（与 `burning_cloud.DECKS` 的气候值一致）。阳光从太空射向云底，只穿过云层以上的大气。若沿用 Kasten & Young 的平面平行空气质量把光算到地面，太阳高度 0° 时空气质量约 37.9、550 nm 透射率仅约 0.024，日落瞬间会被错误地压成接近全黑，而且无法表达"太阳已落到地平线以下、高云仍被点亮"的火烧云事实。
2. **瑞利与气溶胶分别积分**：分别沿前向射线积分球面几何，`m_X = ∫ exp(-z/H_X)·ds/H_X`，其中 `z` 为海拔、`ds` 为路径元。瑞利（分子）标高 `H_R = 8000 m`，气溶胶标高 `H_A = 1500 m`。两者不能共用一个 Chapman 空气质量：Chapman 式只对高度尺度约 `R/2 ≈ 3200 km` 的整层柱成立，而气溶胶集中在 1.5 km 以下，8600 m 高云的光路里几乎没有气溶胶；共用会把 AOD 凭空放大一个量级，把日落压黑。`slant_airmass` 仅保留作快速粗估。
3. **受光极限（地球本影）与软边**：由切线几何得 `α_min(h) = -sqrt(2h/R)`（弧度）。1500 m 约 -1.24°，5200 m 约 -2.31°，8600 m 约 -2.98°。真实太阳视半直径约 0.266°，本影边缘是渐变的，因此透射谱乘以 `smoothstep(α_min - 0.266°, α_min + 0.266°, α)`，避免亮度在本影边缘硬跳变。
4. **瑞利散射**：550 nm 垂直光学厚度 `τ_R,550 = 0.098`，波长依赖为 `τ_R(λ) = τ_R,550·(λ/550)^-4.1`。蓝光被优先散射，太阳越低，透射光越红。
5. **气溶胶米散射**：Ångström 幂律 `τ_A(λ) = AOD550·(λ/550)^-1.3`，指数取约 1.3。本步骤尚未接入 CAMS 气溶胶，AOD550 固定为中性假设值 `0.2`，输出字段 `color_aod_source = "assumed"`。
6. **透射率**：`T(λ) = exp(-(τ_R(λ)·m_R + τ_A(λ)·m_A))·fade`；`m_R`、`m_A` 用各自标高从云层高度起算，射线撞地时返回 0。
7. **配色**：CIE 1931 `x̄/ȳ/z̄` 使用 Wyman, Sloan & Shirley (2013) 的多瓣高斯解析近似；XYZ **直接**经标准 D65 矩阵转为 linear sRGB，gamma 编码并做 gamut 裁剪。不再先按 `Y=1` 归一化取色度再乘亮度，避免低太阳时暖色被灰化。
8. **受光云层与画布**：按高 → 中 → 低顺序选择第一层云量 ≥ 10% 且仍高于自身受光极限的云层，用它的透射谱决定色度。亮度用 CIE `L*`：`clamp(canvas_strength(三层云量) · L*/100, 0, 1)`，其中 `L* = 116·Y^(1/3) - 16`（暗端用 `903.3·Y`）。只有画布强度 `canvas < 0.35` 时才按权重向中性灰蓝收拢；全部云层都进入本影时输出 `#000000` / 中性灰 / 亮度 0。

逐时字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `color_hex` | string | `#rrggbb` 预计颜色 |
| `color_name` | string | 中文色名：金黄 / 橙金 / 橙红 / 玫红 / 暗红 / 灰紫 / 中性灰 / 灰蓝 |
| `color_brightness` | number | 0–1 感知亮度（两位小数） |
| `color_rgb` | array | `[r, g, b]`，0–255 |
| `color_aod_source` | string | 当前固定为 `"assumed"` |
| `color_deck` | string / null | 受光云层：高云 / 中云 / 低云；无则为 `null` |
| `color_deck_height_m` | number / null | 受光云层高度（m），无则为 `null` |

已知局限：

- **气溶胶垂直分布为单一标高近似**：AOD550 目前是固定假设值，气溶胶廓线按 1.5 km 标高指数衰减；真实霾、沙尘和火山气溶胶会显著改变颜色与亮度，后续接入 CAMS AOD 后应替换该假设。
- **无云光学厚度**：云量只决定"有没有画布"和哪层云受光，未计算云滴 / 冰晶的光学厚度、多次散射与云体自身消光。
- **直射光近似**：模型只算太阳直射光的透射谱，不含天空多次散射与气辉；真实低太阳霞光通常比该估算更亮、更弥散。
- **色度近似**：CIE 配色函数为解析拟合，sRGB gamut 裁剪也会损失部分光谱色，颜色仅供氛围参考，不替代实拍或专业色度测量。

### 4.5 合成管线 `core/pipeline.py`

| 常量 | 值 | 含义 |
| --- | ---: | --- |
| `FORECAST_HOURS` | 96 | 默认逐时预报时长 |
| `SUMMARY_DAYS` | 3 | 每日摘要天数 |
| `SNAPSHOT_DAY_COUNT` | 3 | 快照选取天数 |

管线逐机位生成 96 小时序列、最佳日出 / 日落窗口、观赏状态与拍摄朝向建议，最后汇总为一份静态 JSON。

### 4.6 地面观测降级 `_ground_obs`

任何一步失败都不会中断流程：

1. 配置了 `QWEATHER_KEY` 且请求成功 → 能见度与 AQI 使用和风天气，`ground_source=qweather`
2. 否则 → 能见度使用 Open-Meteo 逐时 `visibility`（米转公里），AQI 为 `None`，即不施加气溶胶惩罚，`ground_source=open-meteo`
3. 连逐时数据也缺失 → 中性兜底能见度 15.0 km

`meta.ground_visibility_source` 会记录实际使用的来源，便于排查数据质量。

---

## 5. 数据格式

`public/data/forecast.json` 的顶层键顺序固定：

```text
updated_at
meta
spots[]
grid          # 使用 --no-grid 时为 null
summary[]
```

### 5.1 顶层字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `updated_at` | string | 文档更新时间（UTC ISO 8601） |
| `meta` | object | 运行元信息 |
| `spots` | array | 11 个机位的完整预报 |
| `grid` | object / null | 热力网格；`--no-grid` 时为 `null`，页面不消费该字段 |
| `summary` | array | 未来 3 天的每日摘要 |

### 5.2 `meta`

| 字段 | 说明 |
| --- | --- |
| `name` / `region` / `timezone` | 产品名、区域、时区 |
| `utc_offset_hours` | 时区偏移小时数 |
| `hours` / `spot_count` | 预报小时数与机位数量 |
| `mock` | 是否使用离线确定性数据 |
| `data_source` / `ground_source` / `ground_visibility_source` | 高空、地面、能见度的实际来源 |
| `generated_at` | 本次生成时间 |

### 5.3 `summary[]`

| 字段 | 说明 |
| --- | --- |
| `date` | 日期 |
| `weekday` | 星期 |
| `evening` | 当日黄昏最佳窗口与评分 |
| `morning` | 当日清晨最佳窗口与评分 |
| `sea` | 当日云海概览 |

数组固定 3 天。

### 5.4 `spots[]`

| 字段 | 说明 |
| --- | --- |
| `id` / `name` / `lat` / `lon` / `elevation_m` / `region` | 机位标识与地理信息 |
| `preferences` | 机位能力开关（`sea_of_clouds` / `burning_cloud`）与 `priority` 标注 |
| `orientation` / `recommended_focal_length_mm` | 推荐朝向与焦距 |
| `ground` | 地面观测摘要 |
| `best_evening` / `best_morning` / `best_sea` | 最佳黄昏、清晨、云海时刻 |
| `best_sunset` / `best_sunrise` | 最佳日落 / 日出时刻 |
| `status` / `hourly` | 当前状态与逐时序列 |

### 5.5 `hourly[]`

每个机位 96 条逐时记录，字段如下：

| 字段 | 说明 |
| --- | --- |
| `time` / `time_local` | UTC 与本地时间 |
| `bci` / `bci_grade` / `bci_components` | 总分、分档与三分量 |
| `sci` / `sci_status` / `sci_state` / `sci_components` | 云海指数、中文状态、机读状态与分量 |
| `inversion_height_m` / `inversion_strength` | 逆温层高度与强度 |
| `cloud_base_m` / `cloud_top_m` | 云底与云顶高度 |
| `sun_elevation` / `sun_azimuth` / `sun_hour_angle` / `is_daytime` | 太阳几何与昼夜标记 |
| `cloud_low` / `cloud_mid` / `cloud_high` | 低 / 中 / 高云量 |
| `visibility_km` / `aqi` | 能见度与空气质量指数；无真实 AQI 时为 `null` |
| `ground_source` / `wind_speed_10m` | 地面观测来源与 10 m 风速 |
| `color_hex` / `color_name` / `color_brightness` / `color_rgb` | 预计霞光颜色、中文色名、0–1 亮度与 RGB |
| `color_aod_source` | 气溶胶来源，当前固定为 `assumed` |

---

## 6. 目录结构

```text
sky-forecast/
├─ api/
│  └─ forecast.py               # 可选 Serverless 入口，纯标准库
├─ config/
│  └─ spots.json                # 11 个机位配置
├─ core/
│  ├─ fetcher/
│  │  ├─ __init__.py
│  │  ├─ open_meteo.py          # ECMWF 探空、云量、能见度
│  │  └─ qweather.py            # 和风天气地面观测
│  ├─ physics/
│  │  ├─ __init__.py
│  │  ├─ solar.py               # NOAA 太阳几何
│  │  ├─ burning_cloud.py       # BCI
│  │  ├─ sea_of_clouds.py       # SCI
│  │  └─ spectrum.py            # 直射光透射谱与预计霞光颜色
│  ├─ __init__.py
│  └─ pipeline.py               # 合成管线
├─ public/
│  ├─ index.html                # 自包含单页
│  └─ data/
│     └─ forecast.json          # 构建产物，.gitignore 忽略
├─ scripts/
│  ├─ generate_forecast.py      # 静态数据生成器
│  ├─ serve.py                  # 本地预览服务器
│  └─ manage.sh                 # 预览进程管理
├─ tests/
│  ├─ __init__.py
│  ├─ test_physics.py
│  ├─ test_pipeline.py
│  └─ test_spectrum.py
├─ .github/
│  └─ workflows/
│     └─ forecast.yml           # 生成并部署 GitHub Pages
├─ .run/                        # 本地预览运行时，已 gitignore
│  ├─ serve.pid
│  └─ serve.log
├─ .env.example                 # 环境变量模板，仅占位符
├─ .gitignore
├─ vercel.json                  # 可选部署配置
└─ README.md
```

---

## 7. 本地开发

### 7.1 生成数据

```bash
python3 scripts/generate_forecast.py                  # 默认走真实上游
python3 scripts/generate_forecast.py --mock           # 完全离线，确定性数据
python3 scripts/generate_forecast.py --no-grid        # 跳过网格，减小体积
python3 scripts/generate_forecast.py --hours 48       # 自定义时长
python3 scripts/generate_forecast.py --spots config/spots.json --output /tmp/sky.json
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--mock` | 关闭 | 使用确定性离线数据 |
| `--spots` | `config/spots.json` | 机位配置路径 |
| `--output` | `public/data/forecast.json` | 输出路径 |
| `--hours` | 96 | 预报小时数 |
| `--no-grid` | 关闭 | 不生成网格，`grid` 写为 `null` |

### 7.2 本地预览

```bash
python3 scripts/serve.py
# http://127.0.0.1:8080/
```

默认监听 `127.0.0.1:8080`，可用 `--host`、`--port`、`--dir` 调整，`--no-browser` 阻止自动打开浏览器。响应头固定带上 `Cache-Control: no-store, must-revalidate`，修改后刷新即可看到。

### 7.3 进程管理

```bash
scripts/manage.sh start
scripts/manage.sh status
scripts/manage.sh restart
scripts/manage.sh stop
```

| 子命令 | 作用 |
| --- | --- |
| `start` | 后台启动 `serve.py`，等待端口就绪 |
| `stop` | 停止进程，必要时强制结束 |
| `status` | 显示运行状态与地址 |
| `restart` | 先停后启 |

可用环境变量覆盖默认值：`SKY_FORECAST_HOST`（默认 `127.0.0.1`）、`SKY_FORECAST_PORT`（默认 `8080`）、`PYTHON`（默认 `python3`）。PID 写入 `.run/serve.pid`，日志写入 `.run/serve.log`，整个 `.run/` 已被 `.gitignore` 忽略。

### 7.4 实时 API

```bash
python3 api/forecast.py
curl -s "http://127.0.0.1:8077/api/forecast?hours=24&grid=0"
```

本地默认监听 8077，路由为 `GET /api/forecast`：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `mock` | `0` | 置 1 时强制离线数据 |
| `hours` | 96 | 预报小时数，上限 168 |
| `spots` | 全部 | 逗号分隔的机位 id 子集 |
| `grid` | `1` | 置 0 跳过网格 |
| `step` | `0.1` | 网格分辨率，夹在 0.05–1.0 之间 |

响应在标准文档基础上附加 `api` 字段，记录本次实际参数。

### 7.5 测试

```bash
python3 -m unittest discover -s tests -q
# Ran 84 tests ... OK
```

测试位于 `tests/test_physics.py` 与 `tests/test_pipeline.py`；必要时可附加语法检查：

```bash
python3 -m py_compile api/forecast.py scripts/serve.py scripts/generate_forecast.py
```

---

## 8. 环境变量

复制 `.env.example` 为 `.env` 后填写。只有 `QWEATHER_KEY` 是凭据，其余均可省略。

| 变量 | 必需 | 默认 | 说明 |
| --- | :---: | --- | --- |
| `QWEATHER_KEY` | 否 | 空 | 和风天气 API Key；缺省时自动降级，流程不失败 |
| `QWEATHER_BASE_URL` | 否 | `https://devapi.qweather.com/v7` | 和风天气接口地址 |
| `OPEN_METEO_BASE_URL` | 否 | `https://api.open-meteo.com/v1` | Open-Meteo 接口地址 |
| `TZ` | 否 | 系统时区 | 建议 `Asia/Shanghai`，影响日志与本地时间 |

GitHub Actions 中只需要配置一个 Secret：

```text
Settings → Secrets and variables → Actions → New repository secret
Name: QWEATHER_KEY
Secret: <你的 key>
```

不配置也能正常运行，此时地面观测走 Open-Meteo 降级路径，AQI 为空且不施加气溶胶惩罚。

---

## 9. 部署

### 9.1 GitHub Pages（主路径）

工作流名称为 `Generate & Deploy Sky Forecast`，线上地址 https://nsv2051.github.io/sky-forecast/ 。

| 触发 | 条件 |
| --- | --- |
| `schedule` | `0 */2 * * *`，每 2 小时 |
| `workflow_dispatch` | 可勾选布尔输入 `force_mock` 强制离线数据 |
| `push` | 推送到 `main` / `master`，且命中 `core/**`、`config/**`、`public/index.html`、`scripts/generate_forecast.py`、`.github/workflows/forecast.yml` |

权限与并发：

- `permissions`：`contents: read`、`pages: write`、`id-token: write`
- `concurrency`：group `sky-forecast-pages`，`cancel-in-progress: false`

`build` job 运行在 `ubuntu-latest`，超时 15 分钟，步骤依次为：

1. `actions/checkout@v4`
2. `actions/setup-python@v5`，Python 3.12
3. 升级 pip；仅当存在 `requirements.txt` 时安装依赖
4. 生成数据：`force_mock` 为 true 时执行 `--mock --no-grid`，否则执行 `--no-grid`
5. 校验 JSON：文件非空、`meta.spot_count > 0`、`summary` 非空
6. `actions/configure-pages@v5`，`enablement: true`
7. `actions/upload-pages-artifact@v3`，上传 `public/`

`deploy` job 依赖 `build`，使用 `github-pages` environment，并通过 `actions/deploy-pages@v4` 发布。

生成的数据不进入 git 历史，只作为 Pages artifact 上传。首次启用需要在仓库 `Settings → Pages → Source` 选择 **GitHub Actions**；`GITHUB_TOKEN` 无权创建 Pages 站点，这一步只能由仓库所有者完成。线上首页与仓库中的 `public/index.html` 已校验 md5 一致。

### 9.2 Vercel（可选）

`vercel.json` 保留为可选的第二种部署方式，不是主路径：

- `outputDirectory: "public"`，`cleanUrls: true`，`trailingSlash: false`，`/` 重写到 `/index.html`
- `api/forecast.py` 使用 `python3.12`，内存 256 MB，`maxDuration` 30 秒
- `/data/forecast.json` 响应头 `public, max-age=0, s-maxage=300, stale-while-revalidate=600`
- 全局响应头包含 `X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Frame-Options: SAMEORIGIN`

Pages 没有服务端，页脚的 `api/forecast` 在 Pages 上会 404；需要按需实时重算时再启用 Vercel。

---

## 10. 已知限制

- GitHub 定时任务不保证准点，高负载时可能延迟数分钟到半小时，偶尔会跳过。
- Pages 的 `Cache-Control` 由平台决定，数据刚更新时页面可能短暂显示旧内容。
- 页脚「实时 API」在 Pages 上返回 404，这是缺少服务端导致的预期行为。
- 未配置 `QWEATHER_KEY` 时没有真实 AQI，气溶胶分量为中性，BCI 可能与有观测时略有差异。
- 默认生成网格会增加 JSON 体积；页面不消费 `grid`，线上构建统一使用 `--no-grid`。
- `color_*` 预计色彩使用固定 AOD550 假设值，且不含云光学厚度、多次散射和气溶胶垂直廓线，仅作氛围参考。
- `github-pages` environment 在首次部署时由平台自动创建。

---

## 11. 安全

- 仓库内不存在真实凭据，`.env.example` 只有空占位符。
- `.env` 与 `.env.*` 被 `.gitignore` 忽略，仅放行 `.env.example`。
- `QWEATHER_KEY` 只在运行时从环境变量读取，CI 中通过 GitHub Secrets 注入。
- 前端 HTML / JS 不含任何密钥，数据只从同源静态 JSON 或同源 API 获取。
- `.gitignore` 同时忽略 `public/data/*.json`、`.run/`、`__pycache__`、`.venv` 等本地产物。
- 工作流权限保持最小化：`contents: read`、`pages: write`、`id-token: write`。

---

## 12. 多模式共识与配额成本

生产路径在原有 33 变量单模式请求之外，额外发一次多模式请求，仅取三层云量：

| 常量 | 值 |
| --- | --- |
| `CONSENSUS_MODELS` | `ecmwf_ifs025`, `gfs_seamless`, `icon_seamless` |
| `CONSENSUS_VARIABLES` | `cloud_cover_low`, `cloud_cover_mid`, `cloud_cover_high` |

### 12.1 计费公式

Open-Meteo 按成本而非请求数计费：

```text
每地点 cost = max(1.0, 模型数 × 变量数 / 10.0)
```

- 基线：33 变量 × 1 模型 = 3.30 / 地点，11 个机位合计 36.3 / 轮。
- 共识：3 变量 × 3 模型 = 0.9，被下限抬到 1.0 / 地点，11 个机位合计 11.0 / 轮。
- 合计约 47.3 / 轮；每日 12 轮约 568，占每日 10000 上限约 5.7%。
- `forecast_days` 不影响成本；响应头 `x-ratelimit-cost` 可用于自检。

### 12.2 共识字段

逐时 `hourly[]` 新增（原有字段不变）：

| 字段 | 说明 |
| --- | --- |
| `consensus.low/mid/high` | 每层四个来源（`best_match` + 三个独立模式）的 `values`、`available`、`mean`、`min`、`max`、`spread`、`stdev`、`agreement`、`level`、`cloud_prob`、`model_count` |
| `consensus_score` | 三层一致度按低云 0.25 / 中云 0.35 / 高云 0.40 加权；缺失层剔除后重新归一 |

一致度采用 `100 × max(0, 1 - spread / 60)` 的线性映射，避免云量接近 0 时相对量爆掉。有效来源少于 2 个时 `agreement` / `level` 为 `null`，并标注「有效模式不足」，绝不用单一来源伪造一致度。能参与共识的只有云量：`visibility` 仅 `best_match` 与 `gfs_seamless` 有值，另外两个模式全为 `null`。

`meta.consensus_models` 记录本次实际启用的模式；共识请求失败时为空数组，主预报不受影响。前端数据源芯片按该字段点亮，详情面板在 8 项因子之后展示三层共识均值、离散度与一致度等级，并提供默认收起的各源云量矩阵。

---

## 13. 受光极限判据进入评分层

BCI 的太阳高度闸门不再使用与云层高度无关的固定阈值。中/高云画布高度按云量加权

```text
h = (5200·cloud_mid + 8600·cloud_high) / (cloud_mid + cloud_high)   # 均为 0 时取 8600
α_min = -sqrt(2h / R)                                              # R = 6371000 m
```

太阳低于画布自身的受光极限 `α_min` 时才算入本影；在 `α_min` 之上仍按满分处理。
本影边缘用真实太阳视半直径 0.266° 做渐变，避免亮度在本影边缘硬跳变。
`e >= 0` 的白天段数值与旧实现逐位一致。

前端详情面板的「太阳高度角」副标题同步使用同一画布高度公式：`e < α_min` 时显示
「已入本影·画布不受光」，仅改文案，数值、进度条与配色不变。
