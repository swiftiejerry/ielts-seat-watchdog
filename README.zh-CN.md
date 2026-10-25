# IELTS Seat Watchdog · 雅思考位监控（考位提醒）

也叫「雅思考位守望者」。部署在自己电脑上的 NEEA 雅思考位监控 / 考位提醒工具：由你自己的 Chrome 标签页负责查询考位，一个小型 Docker 控制面负责筛选、去重，考位一放出立刻推送到微信。支持机考与纸笔考，全国 34 个城市、日期、考点全程点选配置，日常使用不改代码。

[English](README.md)

| 控制面板 | 手机微信推送 |
| --- | --- |
| ![控制面板](docs/screenshots/panel-status.png) | ![微信推送](docs/screenshots/push-wechat.png) |

| 日期/场次/考点配置 | 推送配置 + 已发现考位（Token 已打码） |
| --- | --- |
| ![配置面板](docs/screenshots/panel-config.png) | ![推送配置与考位](docs/screenshots/panel-push-found.png) |

## 为什么是这套架构

ielts.neea.cn 对一切非浏览器 HTTP 客户端返回 412，自动化浏览器里的接口调用同样无法稳定工作。**始终可用**的数据通道只有一个：你自己登录着的 Chrome 标签页。所以本项目不与之对抗，而是把活儿拆开：

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  Chrome + 油猴脚本（眼睛）    │  HTTP  │  控制面容器（大脑）            │
│  - 用页面自身 jQuery 调       │ ─────► │  (:9899, Flask)              │
│    queryTestSeats 查考位     │        │  - 配置存储 + Web 面板         │
│  - 回传考位/考点/场次         │ ◄───── │  - 场次/考点过滤              │
│  - 会话过期自动重登（OCR）    │  任务   │  - 去重（seatId + 24h）       │
└─────────────────────────────┘        │  - 微信推送 + 每日心跳         │
                                       └──────────┬───────────────────┘
┌─────────────────────────────┐                   │
│  OCR 打码容器（可选）         │ ◄── 验证码 ───────┘（容器内网）
│  (:9898, ddddocr)           │
└─────────────────────────────┘
```

- **眼睛（油猴脚本）**：跑在你真实浏览器里的 ielts.neea.cn 页面上，用网站自己的方式查考位，向控制面领取"下一步查什么"。因为是你的真实会话，网站看到的就是一个正常用户。
- **大脑（控制面）**：一切可配置的东西都在 `http://127.0.0.1:9899` 的 Web 面板里点选——城市、日期、场次、考点、推送通道。保存即热加载，脚本下一轮自动生效。
- **打码容器（可选）**：会话过期时，脚本抓取登录验证码发给 `ddddocr` 容器识别，自动重新登录。

效果：类 App 的省心体验，零云端、零第三方经手你的账号密码。

## 第二套数据源：IDP（可选模块）

如果你也盯 IDP 的考位，仓库里还有一套**完全独立**的监控（[`idp/`](idp/)）。它和上面那套不是同一个东西的两个副本，而是针对另一个考试系统另起的一套：

| | NEEA（ielts.neea.cn） | IDP（sign.idpielts.cn） |
|---|---|---|
| 防护 | 瑞数动态 WAF，纯 HTTP 一律 412 | 无，裸 HTTP 直接 200 |
| 登录 | 必须，还要过验证码 | 免登录、免验证码 |
| 请求量 | 城市×日期逐个查 | 一次 POST 拿全量 |
| 响应 | 明文 JSON | SM4-CBC 加密 |
| 需要跑什么 | Chrome + 油猴 + OCR 容器 | 一个 Python 容器 |

因为 IDP 没有瑞数那类请求级授权层，这套**不需要浏览器**，纯容器常驻即可，实测能承受 4 req/s 连续请求。判定"有位"用 `fullyBooked` 加上余量双重校验——两个条件都有实测依据，单用任何一个都会误报（详见 [`idp/README.md`](idp/README.md)）。

注意 IDP 大陆目前只有机考，没有纸笔和 UKVI。要考纸笔的话这条路走不通。

```bash
cp idp/config.example.yml idp/config.yml   # 然后填考点与推送 token
docker compose up -d ielts-idp             # 面板在 http://127.0.0.1:9897
```

## 功能

- **点选式配置面板** —— 34 个城市；"扫描真实考期"扫出未来 90 天真正有考试安排的日期；场次（09:00 / 14:00）与考点从真实响应里自动学习。日常使用不碰 YAML。
- **多城市 × 多日期 × 场次/考点过滤** —— 例如"深圳 + 13 个日期 + 上下午两场 + 2 个心仪考点"，全是勾选的事。
- **秒级微信推送** —— pushplus、Server酱、Bark、企业微信 webhook、钉钉、飞书或任意自定义 HTTP 接口。同一考位 24 小时内只推一次。
- **克制的巡查节奏** —— 平峰 300s、夜间 01:00–07:00 降为 10 分钟一查；放位高峰窗口（09:55–10:40 / 19:55–20:40）收紧到 8s ± 2s；登录失败 3 次熔断 30 分钟。只调只读查询接口，绝不自动报名。
- **重启自恢复** —— 容器 `restart: unless-stopped`；重新打开 NEEA 标签页即自动续巡、自动重登。多标签页单实例锁保证一个浏览器只有一个 worker。
- **每日心跳** —— 每天 09:00 一条"还活着"推送；worker 超 5 分钟没上报，面板亮"数据源离线"。

## 快速开始（Docker，推荐）

准备：Docker Desktop、Chrome + Tampermonkey、一个能登录 [ielts.neea.cn](https://ielts.neea.cn) 的账号。

```bash
git clone https://github.com/swiftiejerry/ielts-seat-watchdog.git
cd ielts-seat-watchdog
cp config.example.yml config.yml      # Windows: copy config.example.yml config.yml
# 编辑 config.yml：填入 neea.user_id / neea.password（仅自动重登需要）
docker compose up -d --build
```

然后：

1. 把 [userscript/ielts-seat-watchdog.user.js](userscript/ielts-seat-watchdog.user.js) 装进 Tampermonkey（工具箱 → 导入文件，或用原始地址新建脚本）。
2. 打开 https://ielts.neea.cn 并登录。页面右下角出现小角标（绿色=巡查中，琥珀=重登中，灰色=待机）。
3. 打开控制面板 <http://127.0.0.1:9899>，点选城市、日期、场次、考点和推送通道，点 **保存并测试推送**。

之后只要你的过滤条件内有考位放出，微信立刻收到消息。worker 还会在 NEEA 标签页右下角挂一个小角标——绿色=巡查中，琥珀=重登中，灰色=待机：

![NEEA 页面上的工作角标](docs/screenshots/badge-on-neea.png)

> Windows + Docker Desktop：勾选 *Settings → General → Start Docker Desktop when you sign in*，重启后监控自动恢复。Chrome 里开启 *启动时 → 继续浏览上次打开的页面*，重启后 NEEA 标签页自动回来。

## 不用 Docker 跑

```bash
python -m pip install flask pyyaml tzdata ddddocr pillow flask-cors
# 终端 1（可选，仅自动重登需要）
python ocr_server.py                  # 监听 :9898
# 终端 2（控制面）
MONITOR_CONFIG=/path/to/config.yml MONITOR_DATA=/path/to/data python monitor/ielts_monitor.py
```

非容器化时把 `ocr.url` 改成 `http://127.0.0.1:9898`。

## 配置

日常调整都在 Web 面板里；`config.yml` 只负责首次启动的种子配置。

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `neea.user_id` / `neea.password` | 空 | NEEA 账号密码，仅供油猴脚本自动重登用，不出本机 |
| `monitor.product_code` | `IELTSCDI` | `IELTSCDI`=机考，`IELTSPBT`=纸笔考 |
| `monitor.cities` | `["440300"]` | 城市代码，面板可点选（440300=深圳） |
| `monitor.dates` | `[]` | `YYYY-MM-DD` 列表；面板的 7/14/30 天按钮自动生成 |
| `monitor.center_keywords` / `session_keywords` | 空 | 子串过滤；空 = 全部接受 |
| `monitor.interval_seconds` / `jitter_seconds` | 60 / 30 | 巡查节奏；控制面强制 ≥ 30s |
| `monitor.night_pause` | 01:00–07:00 → 600s | 夜间降频 |
| `push.provider` / `push.token` | pushplus / 空 | 见下方通道列表 |
| `push.heartbeat_time` | `09:00` | 每日存活心跳 |

推送通道：`pushplus`、`serverchan`、`bark`、`wecom-hook`、`dingtalk`、`feishu`、`custom`（POST `{title, message}` JSON 到任意 URL，URL 直接填在 token 栏）。

## 常见问题

- **这是「抢考位」外挂吗？** 不是。只调用只读的考位查询接口，绝不自动报名或支付；巡查节奏随机且克制（平峰 300s，高峰窗口 8s±2s）。
- **需要把账号密码交给第三方吗？** 不需要。所有组件都跑在自己电脑上，凭据不出本机，零云端、零第三方。
- **考位提醒怎么发到微信？** 内置 pushplus、Server酱、Bark、企业微信、钉钉、飞书或任意自定义 HTTP 接口，面板里填 Token 即可；同一考位 24 小时内只推一次。
- **支持纸笔考吗？** 支持。机考 `IELTSCDI`、纸笔考 `IELTSPBT`，面板里切换。
- **和 ielts-auto-booking、ielts.cn_hkj 是什么关系？** 油猴脚本基于前者（MIT，见致谢）重构，并感谢后者的先行探索。差异在于：网页面板点选配置、多通道微信推送、24 小时去重、每日心跳、验证码自动重登，日常使用不用改脚本。

## 公平使用与风控说明

- worker 只调用**只读**的考位查询接口，节奏随机化、远低于人工点选的强度；绝不自动化报名或支付。
- 请保持默认参数。控制面刻意拒绝低于 30 秒的间隔。
- 本项目仅供个人使用，请自行遵守网站服务条款。与 NEEA / 英国文化教育协会无任何关联。

## 测试

```bash
python tests/test_offline.py   # 28 项离线检查：考位解析、日期展开、
                               # 夜间窗口、推送构造、配置合并
python -I idp/test_idp_offline.py   # 56 项：IDP 判定、去重、滚动窗口、配置校验
```

## 目录结构

```
userscript/ielts-seat-watchdog.user.js   眼睛 —— Tampermonkey worker 脚本
monitor/ielts_monitor.py                 大脑 —— Flask 控制面
monitor/ui.html                          Web 面板（单文件，无构建步骤）
ocr_server.py + Dockerfile               ddddocr 验证码打码服务
idp/idp_monitor.py                       独立模块 —— IDP 考位监控
idp/ui.html                              IDP 面板（三种日期模式）
idp/test_idp_offline.py                  IDP 离线测试
docker-compose.yml                       一条命令拉起全部组件
tests/test_offline.py                    离线单元检查
config.example.yml                       种子配置模板
```

## 致谢

油猴脚本基于 SkyBlue997 的 [ielts-auto-booking](https://github.com/SkyBlue997/ielts-auto-booking)（MIT）构建——保留了其登录与轮询基础，重构成 worker 模式并接入控制面协议。同时感谢 [ielts.cn_hkj](https://github.com/mydansun/ielts.cn_hkj) 在这一方向上的先行探索。

## 许可证

[MIT](LICENSE)
