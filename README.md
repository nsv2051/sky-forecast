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
python3 -m unittest discover -s tests -q         # 38 个测试，全部通过
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

### 4.4 合成管线 `core/pipeline.py`

| 常量 | 值 | 含义 |
| --- | ---: | --- |
| `FORECAST_HOURS` | 96 | 默认逐时预报时长 |
| `SUMMARY_DAYS` | 3 | 每日摘要天数 |
| `SNAPSHOT_DAY_COUNT` | 3 | 快照选取天数 |

管线逐机位生成 96 小时序列、最佳日出 / 日落窗口、观赏状态与拍摄朝向建议，最后汇总为一份静态 JSON。

### 4.5 地面观测降级 `_ground_obs`

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
│  │  └─ sea_of_clouds.py       # SCI
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
│  └─ test_pipeline.py
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
# Ran 38 tests ... OK
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
- `github-pages` environment 在首次部署时由平台自动创建。

---

## 11. 安全

- 仓库内不存在真实凭据，`.env.example` 只有空占位符。
- `.env` 与 `.env.*` 被 `.gitignore` 忽略，仅放行 `.env.example`。
- `QWEATHER_KEY` 只在运行时从环境变量读取，CI 中通过 GitHub Secrets 注入。
- 前端 HTML / JS 不含任何密钥，数据只从同源静态 JSON 或同源 API 获取。
- `.gitignore` 同时忽略 `public/data/*.json`、`.run/`、`__pycache__`、`.venv` 等本地产物。
- 工作流权限保持最小化：`contents: read`、`pages: write`、`id-token: write`。
