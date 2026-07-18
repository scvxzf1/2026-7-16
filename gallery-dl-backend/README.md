# gallery-dl 独立代理池后端

这是一个独立 FastAPI 后端：

- `../gallery-dl-codeberg`：始终作为独立子进程执行，源码保持原样；
- `gdl_backend/proxy_*.py`：项目自身的订阅解析、原子租约、冷却管理、HTTP 转发器和隧道节点桥接。

后端代码位于独立目录，gallery-dl 的更新与本后端互不覆盖。

## 已实现能力

- SQLite/WAL 任务、尝试、事件、日志、代理租约和站点策略持久化；
- gallery-dl 子进程队列、全局并发和每站并发限制；
- 单任务固定一个代理节点，代理故障后排除旧节点并换节点重试；
- `direct`、`prefer`、`required` 三种代理策略；
- HTTP/HTTPS/SOCKS 原生代理池，以及 Clash 隧道节点到本地 HTTP 出口的自动桥接；
- Base64、纯文本及 Clash YAML 机场订阅导入；
- 带 Basic Auth 的 HTTP 上游自动包装为任务级本地无认证转发端口；
- 全池探活和站点专用 HTTPS 探活；
- 节点标签/地区筛选、冷却、延迟、成功/失败统计；
- 任务取消、Windows 子进程树回收、重启孤儿任务恢复；
- 日志轮询、事件流、任务文件清单和文件读取；
- API Key、输出目录白名单、配置/Cookie 文件白名单、敏感信息脱敏；
- Idempotency-Key 防止重复提交。

## 架构

```text
HTTP API
  └─ TaskScheduler
      ├─ SQLite (tasks/attempts/logs/events/leases/policies)
      ├─ SitePolicy (并发、探活地址、节点标签、超时、重试)
      ├─ ProxyPoolAdapter
      │   ├─ proxy_sources (机场订阅解析)
      │   ├─ TunnelTransportCore (单核心、多节点本地监听)
      │   ├─ NativeProxyPool (轮换、租约、冷却)
      │   └─ LocalHTTPForwarder (仅认证 HTTP 上游按需启用)
      └─ gdl_backend.worker_entry 子进程
          └─ 从 ../gallery-dl-codeberg import gallery_dl 并运行
```

任务级粘性是默认行为。页面提取和文件下载始终复用同一代理租约，适合 Cookie、登录会话及带签名媒体链接。

## 安装

```powershell
cd .\gallery-dl-backend
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

## 配置节点

在 `config.json` 中填写订阅后，后端启动时自动拉取、解析、去重并探活代理：

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
    "transport_core_binary": "bin/proxy-core.exe",
    "transport_core_sha256": "a3799f2d75c623a7c6d307e1faf88269e24dd746c59df3e9f1c84d5cfbff6c92",
    "transport_core_base_port": 29000
  }
}
```

也可以使用本地节点文件：

```json
{
  "proxy": {
    "node_file": "../CordCloud_Clash_1780410017.yaml",
    "transport_core_enabled": true
  }
}
```

节点文件格式见 `nodes.example.txt`。订阅 URL 和认证信息经过脱敏，不会出现在任务日志中。

原生路径直接接收 HTTP/HTTPS 以及无认证 SOCKS 节点。带认证 HTTP/HTTPS 代理由本地转发器隐藏凭据；带认证 SOCKS 节点计入 `skipped_nodes`，避免把凭据放入 gallery-dl 子进程命令行。

Clash YAML 中的 VLESS、Hysteria2、AnyTLS、Trojan、VMess、Shadowsocks、Mieru 节点由 `proxy_core` 生成“一节点一监听”的本地 HTTP 出口，再统一进入 `NativeProxyPool`。项目内的 `bin/proxy-core.exe` 是经官方发布 SHA-256 校验的 Mihomo compatible 构建；每次启动还会按 `transport_core_sha256` 重新核验文件。启动、配置校验、监听就绪检测和关闭均随后端生命周期自动执行，用户侧只运行后端命令。核心状态位于 `/api/v1/proxy/status` 的 `transport_core` 字段。

## 启动

```powershell
.\run_backend.ps1
```

或者：

```powershell
python -m gdl_backend --config .\config.json
```

默认地址：

- API：`http://127.0.0.1:8787/api/v1`
- Swagger：`http://127.0.0.1:8787/docs`
- 健康检查：`/healthz`
- 就绪检查：`/readyz`

若配置了 `server.api_key` 或环境变量 `GDL_BACKEND_API_KEY`，调用 `/api/v1/*` 时添加：

```text
X-API-Key: YOUR_KEY
```

服务默认绑定回环地址，并拒绝任务/探活目标解析到回环、私网、链路本地或保留 IP。确需抓取局域网图站时，在本地部署的 `server` 配置中设置 `"allow_private_targets": true`。监听地址扩展到非回环接口时，启动校验要求同时配置 API Key。

## 常用 API

### 代理池生命周期接口

代理池默认随后端自动启动。以下接口用于运行期间手动刷新、探活或重载：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8787/api/v1/proxy/start `
  -ContentType application/json `
  -Body '{"force_refresh":true}'
```

相关接口：

```text
GET  /api/v1/proxy/status
POST /api/v1/proxy/start
POST /api/v1/proxy/reload
POST /api/v1/proxy/probe
POST /api/v1/proxy/stop
```

站点探活示例：

```json
POST /api/v1/proxy/probe
{
  "site": "pixiv"
}
```

### 创建下载任务

```powershell
$body = @{
  url = "https://www.pixiv.net/artworks/123456"
  proxy_mode = "required"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8787/api/v1/tasks `
  -Headers @{ "Idempotency-Key" = "pixiv-123456" } `
  -ContentType application/json `
  -Body $body
```

任务接口：

```text
POST /api/v1/tasks
GET  /api/v1/tasks
GET  /api/v1/tasks/{id}
POST /api/v1/tasks/{id}/cancel
POST /api/v1/tasks/{id}/retry
GET  /api/v1/tasks/{id}/logs
GET  /api/v1/tasks/{id}/events
GET  /api/v1/tasks/{id}/files
GET  /api/v1/tasks/{id}/files/{relative_path}
```

### 站点策略

```json
PUT /api/v1/sites/policies/pixiv
{
  "max_concurrency": 2,
  "retry_limit": 2,
  "backoff_base_seconds": 2,
  "proxy_mode": "required",
  "probe_url": "https://www.pixiv.net/",
  "probe_before_use": true,
  "node_tags": ["jp"],
  "http_timeout": 30,
  "gallery_retries": 2,
  "task_timeout_seconds": 3600,
  "extra_args": []
}
```

站点名优先使用 gallery-dl extractor 的 `category`。通用提取器则回退到域名。节点标签来自节点名称、协议和常见地区别名，例如 `JP/日本/🇯🇵` 都会生成 `jp` 标签。

代理模式：

- `direct`：始终直连；
- `prefer`：健康节点可用时走代理，代理池降级时直连；
- `required`：必须取得健康节点，节点缺失时按策略重试。

## 凭据与 Cookie

API 支持 `cookies_file`、`config_file` 和 `credentials_ref`。文件路径必须位于配置的白名单目录。

`credentials_ref: "pixiv_main"` 对应环境变量：

```powershell
$env:GDL_CREDENTIAL_PIXIV_MAIN_USERNAME = "USER"
$env:GDL_CREDENTIAL_PIXIV_MAIN_PASSWORD = "PASSWORD"
```

凭据通过子进程环境注入，在 worker 内部追加到 gallery-dl 参数，后端日志及数据库执行脱敏处理。

## 失败处理

- ProxyError、CONNECT tunnel、407、TLS/SSL 握手和连接拒绝：节点标记失败并进入冷却；
- 429、502、503、504：站点级临时错误，重试但不处罚节点；
- 登录/授权错误、输入错误、不支持 URL、资源不存在：直接进入终态；
- 公共 EH 画廊在特定出口返回 `Insufficient privileges`：冷却该出口并换节点重试；
- gallery-dl 下载 I/O 错误：按任务策略重试；
- 后端重启：校验进程 marker，回收遗留 worker；尚有次数的任务重新排队；
- 用户取消：先发送进程组中断，超时后终止整个子进程树。

## 测试

```powershell
python -m unittest discover -s tests -v
```

上述回归测试使用本地夹具、mock 或 gallery-dl `--version`。

EH 搜索、20 并发代理租约和单张 resample 实网烟雾：

```powershell
python scripts\eh_resample_smoke.py `
  --config config.json `
  --query '"clover days"' `
  --concurrency 20
```

脚本先一次性排队 20 个任务，再启动调度器形成并发屏障；其中 19 个任务执行搜索与首个子画廊模拟提取，1 个任务实际下载首张 resample，并在独立 runtime 目录写入 `report.json`。

EH 精确搜索、整本 resample、图片级真实 20 并发：

```powershell
python scripts\eh_full_gallery_parallel.py `
  --config config.json `
  --query '"clover days"' `
  --concurrency 20 `
  --timeout 1800
```

脚本先由 gallery-dl 搜索并选取首个匹配画廊，再读取画廊索引中的每个图片页地址，为每张图片建立一个真实下载任务。调度器最多同时运行 20 个 gallery-dl worker，各任务独占代理租约，但写入同一个画廊目录；文件名中的 `gid + 页码` 保证互不覆盖。最终 `report.json` 会记录预期页数、实际页数、缺页/重页、全部文件 SHA-256、聚合清单 SHA-256、并发峰值及代理节点统计。
