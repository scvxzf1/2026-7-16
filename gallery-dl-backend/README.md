# gallery-dl 独立代理池后端

这是一个 FastAPI 后端，用于统一搜索图库地址、按选择顺序建立图片任务，并通过独立
代理租约运行 gallery-dl：

- `../gallery-dl-codeberg` 保持为上游 Git submodule，并始终通过独立子进程调用；
- `gdl_backend/` 负责搜索、规划、调度、授权、代理池和状态持久化；
- `/ui/` 提供随后端打包的静态操作界面，无需单独构建前端。

## 平台支持

| 平台 | 支持等级 | 说明 |
| --- | --- | --- |
| Windows | 完整支持 | 主要开发与端到端验证平台，提供 PowerShell 安装和启动脚本。 |
| Linux | 完整支持 | 已验证 API/UI、桌面 Chrome 授权、SQLite、子进程回收和 Mihomo 生命周期。 |
| macOS | 兼容预览 | 具备 POSIX 基础实现；孤儿进程识别仍有 Linux `/proc` 依赖。 |

站点授权需要桌面环境中的 Chrome、Chromium 或 Chromium Browser。非标准路径通过
`auth.chrome_executable` 配置。

## 主要能力

- 搜索 Danbooru 与 E-Hentai/ExHentai，并从 Danbooru 画师资料补充已验证的 X/Pixiv 账号；
- 按来源和地址顺序执行批次，当前地址内部采用图片级并发；
- 使用 SQLite/WAL 持久化任务、尝试、事件、日志、租约和批次进度；
- 导入原生 HTTP/HTTPS/SOCKS 代理及常见机场订阅格式；
- 通过 Mihomo 将 VLESS、VMess、Trojan、Shadowsocks、Hysteria、TUIC 等节点桥接为本地 HTTP 出口；
- 对每个新地址执行站点探活，图片任务全程固定一个代理节点；
- 托管 X、Pixiv、EH 的项目专属浏览器授权，Danbooru 公共抓取无需登录；
- 为 EH/EHX 批次显式选择 `fullimg` 原图或 1280 查看图，并控制 GP 响应时停止或降级；
- 支持任务取消、失败重试、重启恢复、文件清单和幂等提交。

具体进程边界、状态机、搜索证据规则和代理选择算法见
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)。

## 快速开始

先在仓库根目录初始化上游子模块：

```bash
git submodule update --init --recursive
```

Linux：

```bash
cd gallery-dl-backend
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
bash scripts/install_mihomo.sh
cp config.example.json config.json
bash run_backend.sh
```

Windows PowerShell：

```powershell
cd .\gallery-dl-backend
python -m pip install -r requirements.txt
.\scripts\install_mihomo.ps1
Copy-Item config.example.json config.json
.\run_backend.ps1
```

安装脚本固定下载并校验受支持的 Mihomo 版本。自定义目录、强制重装及手动安装见
[`docs/MIHOMO.md`](./docs/MIHOMO.md)。

## 最小配置

编辑 `config.json`，至少设置代理来源。完整字段及默认值以
[`config.example.json`](./config.example.json) 为准：

```json
{
  "proxy": {
    "enabled": true,
    "auto_start": true,
    "engine": "native",
    "allow_socks": true,
    "subscription_urls": [
      "https://SUBSCRIPTION_URL"
    ],
    "transport_core_enabled": true,
    "transport_core_base_port": 29000
  }
}
```

也可以用 `proxy.node_file` 指向本地节点文件，格式示例见
[`nodes.example.txt`](./nodes.example.txt)。后端完整导入节点后统一探活，不限制导入数量。

默认从项目 `bin/` 和系统 `PATH` 查找 Mihomo。其他位置使用
`proxy.transport_core_binary`；需要固定校验本地可执行文件时，再设置
`proxy.transport_core_sha256`。

## 启动与入口

Linux 启动脚本优先使用 `.venv/bin/python`，并透传 `--config`、`--host` 和 `--port`：

```bash
bash run_backend.sh --config ./config.json --port 8788
```

Windows 可运行 `run_backend.ps1`，也可以直接执行：

```powershell
python -m gdl_backend --config .\config.json
```

默认入口：

| 用途 | 地址 |
| --- | --- |
| WebUI | `http://127.0.0.1:8787/ui/` |
| API | `http://127.0.0.1:8787/api/v1` |
| Swagger | `http://127.0.0.1:8787/docs` |
| 健康检查 | `http://127.0.0.1:8787/healthz` |
| 就绪检查 | `http://127.0.0.1:8787/readyz` |

服务只允许绑定回环地址。任务和探活目标默认拒绝回环、私网、链路本地及保留 IP；本地部署
确需访问局域网图站时，在 `server` 配置中设置 `"allow_private_targets": true`。

## WebUI 工作流

1. 查看代理池状态，按需启动、重载或探活。
2. 在“站点登录授权”中完成所需站点登录。
3. 输入关键词搜索来源，核对候选和弱证据，并调整来源/地址顺序。
4. 设置单地址图片并发数，提交批次并查看地址与图片任务状态。

界面直接调用下述 API；搜索归并、执行顺序、代理租约和任务状态均以后端数据库为准。

## 托管授权

| 站点 | 授权方式 | 托管结果 |
| --- | --- | --- |
| X | 项目专属 Chrome 登录 | X/Twitter Cookie |
| Pixiv | 项目专属 Chrome OAuth | 后端专用 gallery-dl cache |
| EH | 项目专属 Chrome 登录 | E-Hentai/ExHentai Cookie |
| Danbooru | 公共 API | 无需登录 |

X、Pixiv 和 EH 共用项目目录中的持久 Chrome Profile，但每次授权使用独立标签页。后端只
管理这个 Profile，不读取用户日常浏览器数据。搜索、规划或下载实际返回认证错误后，凭证
才会标记为失效；重新授权成功后，尚未运行的同站任务会继续调度。

删除单站授权只删除该站导出的 Cookie 或 Token；删除共享浏览器 Profile 会停止授权会话并
清理浏览器状态，但不会自动删除已经导出的站点凭证。权限、缓存交换和失效恢复细节见
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md#进程边界)。

## API 概览

请求和响应模型以运行中的 [Swagger](http://127.0.0.1:8787/docs) 为准。主要端点：

```text
GET  /api/v1/proxy/status
POST /api/v1/proxy/start
POST /api/v1/proxy/reload
POST /api/v1/proxy/probe
POST /api/v1/proxy/stop

GET    /api/v1/auth
GET    /api/v1/auth/{site}
POST   /api/v1/auth/{site}/login/start
GET    /api/v1/auth/{site}/login/{session_id}
DELETE /api/v1/auth/{site}/login/{session_id}
DELETE /api/v1/auth/{site}

GET  /api/v1/search/sites
POST /api/v1/search

POST /api/v1/crawls
GET  /api/v1/crawls
GET  /api/v1/crawls/{batch_id}
GET  /api/v1/crawls/{batch_id}/tasks
POST /api/v1/crawls/{batch_id}/cancel

POST /api/v1/tasks
GET  /api/v1/tasks
GET  /api/v1/tasks/{id}
POST /api/v1/tasks/{id}/cancel
POST /api/v1/tasks/{id}/retry
GET  /api/v1/tasks/{id}/logs
GET  /api/v1/tasks/{id}/events
GET  /api/v1/tasks/{id}/files

PUT /api/v1/sites/policies/{site}
```

Pixiv OAuth 和共享 Profile 清理另有专用授权端点，可直接从 Swagger 或 WebUI 调用。

### 搜索

`POST /api/v1/search`：

```json
{
  "keyword": "artist name",
  "sites": ["danbooru", "x", "pixiv", "eh"],
  "limit": 20,
  "proxy_mode": "required"
}
```

响应按请求中的站点顺序返回 `sources[]`：

- `addresses[]` 保存默认可选的已验证账号/标签地址和 EH 画廊候选；
- `weak_evidence[]` 保存 Danbooru 仅别名匹配、尚未闭环的画师候选；
- `related_profiles` 保存 Danbooru 人工维护的其他平台主页；
- EH 候选带标题、封面、页数和按官方 namespace 分组的 `tag_facets[]`。

详细匹配与过滤规则见
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md#跨来源发现与选择)。

### 顺序批次

客户端从搜索响应中选择地址后，按期望顺序提交到 `POST /api/v1/crawls`：

```json
{
  "sources": [
    {
      "site": "danbooru",
      "addresses": [
        {
          "address_type": "artist_tag",
          "label": "artist name",
          "url": "https://danbooru.donmai.us/posts?tags=artist_name"
        }
      ]
    },
    {
      "site": "pixiv",
      "addresses": [
        {
          "address_type": "account",
          "label": "Artist",
          "url": "https://www.pixiv.net/users/USER_ID/artworks"
        }
      ]
    },
    {
      "site": "eh",
      "addresses": [
        {
          "address_type": "gallery",
          "label": "Gallery",
          "url": "https://e-hentai.org/g/GID/TOKEN/"
        }
      ],
      "eh_download": {
        "image_mode": "original",
        "gp_policy": "stop"
      }
    }
  ],
  "concurrency": 20,
  "max_tasks": 10000,
  "proxy_mode": "required"
}
```

来源和地址顺序串行推进，只有当前地址内部的图片任务并发。每个新地址开始前会进行一次
站点探活，并把通过节点集合持久化；该地址的规划与下载只从此集合取得租约。`concurrency`
还受全局调度上限限制，`max_tasks` 限制整个批次的媒体任务规模。

EH/EHX 来源的 `eh_download.image_mode` 接受 `original` 或 `resample`。原图模式下，
`gp_policy=stop` 保持严格原图并在 GP 响应时停止，`gp_policy=resized` 允许 gallery-dl
降级为 1280 查看图。WebUI 默认提交 `original + stop`。

### 代理策略

- `direct`：始终直连；
- `prefer`：有健康节点时使用代理，代理池降级时直连；
- `required`：必须取得健康节点，否则按站点策略重试。

站点策略可配置并发、重试、探活地址、节点标签和任务超时。完整字段由
`PUT /api/v1/sites/policies/{site}` 的 Swagger 模型定义。

## 失败与恢复

- 明确的代理连接、CONNECT、407 或 TLS 故障会处罚并冷却当前节点；
- 429、502、503、504 作为站点临时错误重试，不处罚节点；
- 认证错误会标记对应托管凭证失效，并暂停尚未运行的同站任务；
- 输入错误、不支持 URL 和资源不存在直接进入终态；
- 后端重启会核验 worker marker、回收遗留进程并重新排队仍可重试的任务；
- 用户取消先中断进程组，超时后终止整个子进程树。

## 测试与诊断

完整单元回归：

```bash
python -m unittest discover -s tests -v
```

测试默认使用本地夹具、mock 或 gallery-dl `--version`。需要真实凭据和代理节点的 EH
烟雾脚本见 [`scripts/README.md`](./scripts/README.md)。
