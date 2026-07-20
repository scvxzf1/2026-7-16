# gallery-dl 独立代理池后端

这是一个独立 FastAPI 后端：

- `../gallery-dl-codeberg`：始终作为独立子进程执行，源码保持原样；
- `gdl_backend/proxy_*.py`：项目自身的订阅解析、原子租约、冷却管理、HTTP 转发器和隧道节点桥接。

后端代码位于独立目录，gallery-dl 的更新与本后端互不覆盖。

## 平台支持

当前版本的平台支持等级如下：

| 平台 | 支持等级 | 说明 |
| --- | --- | --- |
| Windows | 完整支持 | 当前主要开发与端到端验证平台；提供 Mihomo 自动下载脚本和 `run_backend.ps1`。 |
| Linux | 完整支持 | 面向带桌面环境的 Linux；已验证 API/UI、Chrome 授权窗口、SQLite、POSIX 权限、子进程组回收和 Mihomo 生命周期，并提供自动下载脚本。 |
| macOS | 兼容预览 | 具备与 Linux 相同的 POSIX 基础实现，也需要自行配置代理核心和 Chrome 路径。当前陈旧进程识别仍依赖 Linux `/proc`，重启后的孤儿任务清理尚待适配。 |

因此，当前对外支持等级为：**Windows 与 Linux 完整支持，macOS 兼容预览**。

## 已实现能力

- SQLite/WAL 任务、尝试、事件、日志、代理租约和站点策略持久化；
- X/Twitter、Pixiv、Danbooru、EH 的统一关键词搜索、身份证据分级与可选图库地址返回；
- EH 通用搜索候选通过 gdata 批量补齐标题、封面、页数和标签，并返回官方 namespace
  分组的 `tag_facets[]`；
- Danbooru 人工维护的画师其他平台主页归并，并识别可爬取的 X/Pixiv 账号；
- 来源顺序、来源内地址顺序持久化执行，单地址内部统一采用图片级高并发；
- gallery-dl 子进程队列、全局并发和每站并发限制；
- 单任务固定一个代理节点，代理故障后排除旧节点并换节点重试；
- `direct`、`prefer`、`required` 三种代理策略；
- HTTP/HTTPS/SOCKS 原生代理池，以及 Clash 隧道节点到本地 HTTP 出口的自动桥接；
- Base64、纯文本、Clash YAML/JSON、sing-box JSON 及 SIP008 机场订阅导入；
- 带 Basic Auth 的 HTTP 上游自动包装为任务级本地无认证转发端口；
- 全池探活和站点专用 HTTPS 探活；
- 节点标签/地区筛选、冷却、延迟、成功/失败统计；
- 任务取消、Windows 子进程树回收、重启孤儿任务恢复；
- 日志轮询、事件流、任务文件清单和文件读取；
- 本机回环监听、输出目录白名单、配置/Cookie 文件白名单；
- Idempotency-Key 防止重复提交。

## 架构

```text
HTTP API
  ├─ DiscoveryService (跨来源关键词搜索、图库地址/画师外链归并)
  ├─ OrderedCrawlManager (来源顺序、地址顺序、重启恢复)
  ├─ CrawlPlanner (当前地址的图片级任务规划)
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

图片任务使用全程粘性代理租约。关键词搜索、账号/标签作品枚举和 EH 索引规划也从同一个代理池取得短期租约；Mihomo 隧道节点与原生 HTTP/HTTPS/SOCKS 节点使用相同的调度接口。当前地址的全部图片结束后，顺序管理器才激活下一个地址。
Danbooru 的普通提取路径若被 Cloudflare 检查或封禁画师的移除页截断，后端会在同一代理租约体系内改用浏览器 TLS 指纹访问公开 JSON API；画师目录查询不再使用会触发全表扫描的首尾通配符。

## 安装

Linux：

```bash
cd ./gallery-dl-backend
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
bash ./scripts/install_mihomo.sh
cp config.example.json config.json
```

站点授权中心支持 Google Chrome、Chromium 及 Chromium Browser。应从桌面终端启动
后端；授权时项目专属浏览器窗口会直接出现在当前桌面会话。非标准安装位置可通过
`auth.chrome_executable` 指定。

Windows PowerShell：

```powershell
cd .\gallery-dl-backend
python -m pip install -r requirements.txt
.\scripts\install_mihomo.ps1
Copy-Item config.example.json config.json
```

### 自动安装 Mihomo

仓库不携带 Windows 或 Linux 的 Mihomo 可执行文件。上述两个脚本固定下载官方
`v1.19.28` release，根据 `amd64`、`arm64` 或 `386` 自动选择资产，下载后先核验脚本中
固定的归档 SHA-256，再解压、执行 `-v` 并打印最终可执行文件的 SHA-256。

默认目标为：

- Linux：`bin/proxy-core`；
- Windows：`bin/proxy-core.exe`。

后端会自动发现这两个文件。重复安装相同版本会直接成功退出；替换其他版本使用：

```bash
bash ./scripts/install_mihomo.sh --force
```

```powershell
.\scripts\install_mihomo.ps1 -Force
```

自定义安装目录分别使用 `--install-dir PATH` 和 `-InstallDir PATH`。自定义目录需要位于
`PATH`，或在配置中通过 `proxy.transport_core_binary` 指向安装后的文件。

### 手动安装 Mihomo

从 [Mihomo v1.19.28 release](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.28)
下载与平台和架构匹配的资产，并在解压前核验归档 SHA-256：

| 平台 | 架构 | release 资产 | 归档 SHA-256 |
| --- | --- | --- | --- |
| Linux | amd64/x86_64 | `mihomo-linux-amd64-compatible-v1.19.28.gz` | `70d01cfb8cb7bf7a92fd1af16cb4b9553d90bb4eecde3b5c4849103e27c80ddb` |
| Linux | arm64/aarch64 | `mihomo-linux-arm64-v1.19.28.gz` | `2474450cd1c41dfa53036a54a4e85579f493d3af524d86c3d4b8e2b240b56cd2` |
| Linux | 386 | `mihomo-linux-386-v1.19.28.gz` | `d1d3136bf4a8268bd3c182be976ad10747b1be5f74529ee894434742960915fe` |
| Windows | amd64/x86_64 | `mihomo-windows-amd64-compatible-v1.19.28.zip` | `6d8a079d01b3631e73e56b7b42a067afc14f9e3ad99f2880d38bb141cf8fcbe7` |
| Windows | arm64 | `mihomo-windows-arm64-v1.19.28.zip` | `25cedfb999864e834a3d8424cb8ea61b9145b3cb3aea0180b9fdc009623abeda` |
| Windows | 386/x86 | `mihomo-windows-386-v1.19.28.zip` | `1cc14bdde317b38b861569c1d2aaacaf49907c1707b5aa38838e549b451549b1` |

以下命令以常见的 amd64/x86_64 为例；其他架构替换为表中对应的资产名和摘要。
Linux 手动安装到项目目录：

```bash
asset="./mihomo-linux-amd64-compatible-v1.19.28.gz"
expected="70d01cfb8cb7bf7a92fd1af16cb4b9553d90bb4eecde3b5c4849103e27c80ddb"
printf '%s  %s\n' "$expected" "$asset" | sha256sum -c -
mkdir -p ./bin
gzip -dc "$asset" > ./bin/proxy-core
chmod 0755 ./bin/proxy-core
./bin/proxy-core -v
```

Windows PowerShell 手动安装到项目目录：

```powershell
$Asset = ".\mihomo-windows-amd64-compatible-v1.19.28.zip"
$Expected = "6d8a079d01b3631e73e56b7b42a067afc14f9e3ad99f2880d38bb141cf8fcbe7"
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Asset).Hash.ToLowerInvariant()
if ($Actual -ne $Expected) { throw "Archive SHA-256 mismatch: $Actual" }
Expand-Archive -LiteralPath $Asset -DestinationPath .\mihomo-release -Force
New-Item -ItemType Directory .\bin -Force
$Executables = @(Get-ChildItem .\mihomo-release -Recurse -File -Filter *.exe)
if ($Executables.Count -ne 1) { throw "Expected one executable, found $($Executables.Count)" }
Move-Item -LiteralPath $Executables[0].FullName -Destination .\bin\proxy-core.exe -Force
.\bin\proxy-core.exe -v
```

摘要校验通过后再继续解压。也可以把可执行文件安装到系统 `PATH`；后端在没有显式配置
路径时，依次查找项目 `bin` 目录和 `PATH`。兼容名称
`mihomo(.exe)`、`verge-mihomo(.exe)` 仍可被发现。

## 配置节点

在 `config.json` 中填写订阅后，后端启动时先完整拉取、解析并加载全部节点，再对运行池执行探活。
节点没有数量上限，导入多少就加载和探活多少；`probe_timeout_seconds` 默认是 10 秒：

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

未填写 `transport_core_binary` 时，Windows/Linux 先从项目 `bin` 目录查找，再从
`PATH` 查找。需要使用其他位置或在每次核心启动前固定校验文件时，可同时设置绝对路径和
可执行文件摘要：

```json
{
  "proxy": {
    "transport_core_binary": "/usr/local/bin/mihomo",
    "transport_core_sha256": "SHA256_OF_INSTALLED_BINARY"
  }
}
```

`transport_core_sha256` 校验的是解压后的可执行文件，不是上表中的 release 归档。
自动安装脚本会打印该值；手动安装可用 `sha256sum ./bin/proxy-core` 或
`(Get-FileHash -Algorithm SHA256 .\bin\proxy-core.exe).Hash.ToLowerInvariant()` 取得。

也可以使用本地节点文件：

```json
{
  "proxy": {
    "node_file": "../subscriptions/airport.yaml",
    "transport_core_enabled": true
  }
}
```

节点文件格式见 `nodes.example.txt`。

原生路径直接接收 HTTP/HTTPS 以及无认证 SOCKS 节点。带认证 HTTP/HTTPS 代理由本地转发器承接，带认证 SOCKS5 节点通过传输核心桥接。

常见分享 URI、Clash 与 sing-box 中的 VLESS、VMess、Trojan、Shadowsocks/SSR、Hysteria/Hysteria2、TUIC、AnyTLS、Mieru 节点由 `proxy_core` 生成“一节点一监听”的本地 HTTP 出口，再统一进入 `NativeProxyPool`。所有平台都可以通过 `transport_core_sha256` 锁定所选核心。启动、配置校验、监听就绪检测和关闭均随后端生命周期自动执行，用户侧只运行后端命令。核心状态位于 `/api/v1/proxy/status` 的 `transport_core` 字段。节点状态中的 `healthy` 表示最近一次探活成功，`retry_eligible` 只表示冷却已经结束、当前可以再次尝试，两者互不替代。

## 启动

Linux：

```bash
bash ./run_backend.sh
```

脚本会优先使用项目的 `.venv/bin/python`，也可通过 `PYTHON=/path/to/python` 指定其他
解释器；`--config`、`--host`、`--port` 参数会原样传给后端，例如
`bash ./run_backend.sh --config ./config.json --port 8788`。

Windows PowerShell：

```powershell
.\run_backend.ps1
```

或者：

```powershell
python -m gdl_backend --config .\config.json
```

默认地址：

- API：`http://127.0.0.1:8787/api/v1`
- 聚合爬取测试台：`http://127.0.0.1:8787/ui/`
- Swagger：`http://127.0.0.1:8787/docs`
- 健康检查：`/healthz`
- 就绪检查：`/readyz`

服务仅允许绑定回环地址，并拒绝任务/探活目标解析到回环、私网、链路本地或保留 IP。确需抓取局域网图站时，在本地部署的 `server` 配置中设置 `"allow_private_targets": true`。

### 聚合爬取测试台

`/ui/` 是随 Python 包一起提供的纯静态测试界面，无需 Node.js 或前端构建步骤。它覆盖：

- 代理池状态、启动、重载、探活和停止；
- 统一的站点登录授权中心：X、Pixiv、EH 共用一个项目授权 Chrome Profile，
  X/EH 自动导出 Cookie，Pixiv 使用托管 OAuth，Danbooru 公共 API 自动就绪；
- X、Pixiv、Danbooru、EH 聚合关键词搜索；
- 按来源查看并勾选账号、标签或画廊地址；EH 直接展示站内候选的封面、标题、页数和标签，并可按
  官方 namespace 进行包含/排除过滤，X/Pixiv
  身份未匹配项可显式展开；
- 用上下按钮调整来源顺序及来源内部地址顺序；
- 以指定图片并发数提交顺序批次；
- 轮询批次、当前地址和图片任务状态，并支持取消批次；
- 展示 Danbooru 人工维护的其他活动平台主页和原始 API 响应。

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

### 站点登录授权

WebUI 的“站点登录授权”替代手工 Cookie/config 文件输入。常用接口：

```text
GET    /api/v1/auth
GET    /api/v1/auth/{site}
POST   /api/v1/auth/{site}/login/start
GET    /api/v1/auth/{site}/login/{session_id}
DELETE /api/v1/auth/{site}/login/{session_id}
POST   /api/v1/auth/pixiv/oauth/start
DELETE /api/v1/auth/pixiv/oauth/session
DELETE /api/v1/auth/browser-profile
DELETE /api/v1/auth/{site}
```

- X：在 `credentials/managed/browser-profiles/shared/` 对应的共享项目 Chrome 中登录，
  后端导出 `x.com`/`twitter.com` Cookie；
- EH：复用同一个共享 Profile，后端导出 E-Hentai/ExHentai Cookie；
- Pixiv：复用同一个共享 Profile，在 OAuth 页面导航前启用 CDP Network 监听，自动捕获
  callback 并把 refresh-token 写入后端专用 gallery-dl cache；
- Danbooru：当前画师/角色标签搜索和公共作品抓取直接使用公开 API。

X、Pixiv、EH 的 Cookie、本地存储、设备历史和验证状态都由共享 Profile 持久化。授权完成时只关闭
本次标签页，Chrome 宿主继续运行并供下一个站点复用；后端退出时关闭宿主进程而保留 Profile。
所有流程均不读取用户日常浏览器的数据，也没有浏览器选择、Cookie 数据库导入或手工 Cookie
同步流程。只有搜索、规划或下载实际返回
认证错误时，后端才把对应凭证标记为失效；WebUI 显示重新授权提示，尚未启动的同站托管任务
保留在队列，重新登录成功后自动恢复调度。

Chrome 默认从系统路径自动发现；便携版可通过 `auth.chrome_executable` 指定。登录窗口等待时间和
CDP 轮询间隔分别由 `auth.browser_login_timeout_seconds`、
`auth.browser_poll_interval_seconds` 配置。
共享授权浏览器使用随机非零的回环调试端口；启动参数和页面脚本均不修改浏览器指纹。
重新授权直接复用共享 Profile 中的站点状态。
X 仅在 `auth_token`、`ct0` 均已生成且页面离开登录流和 `/account/access` 检查页后才持久化成功状态。

Pixiv 登录授权不要求填写 Token 或复制 callback。开始授权后，后端在共享项目 Chrome 的
目标页上先启用 Network 监听再导航到登录页；登录产生的短时 callback 会被即时捕获、交换并保存，
随后自动关闭本次标签页。API 响应、任务数据库和事件日志均不包含授权码或 refresh-token。
共享窗口保持真实的宿主 Chrome UA、GPU、屏幕、时区和硬件信息。
交换使用单次会话隔离 cache，成功后才更新现有登录；取消或交换失败会清理临时 cache，
并保留此前有效登录。

`DELETE /api/v1/auth/{site}` 只删除该站后端导出的 Cookie 或 Token，共享 Profile 中的浏览器登录
状态继续保留。`DELETE /api/v1/auth/browser-profile` 会先停止所有授权会话和共享 Chrome，再删除
整个共享 Profile；已经导出的站点凭证仍通过各站点接口分别管理。

站点探活示例：

```json
POST /api/v1/proxy/probe
{
  "site": "pixiv"
}
```

### 跨来源关键词搜索

支持的站点和别名：

```text
twitter: x, twitter
pixiv:   pixiv
danbooru: danbooru
exhentai: eh, exhentai, e-hentai
```

搜索接口：

```text
GET  /api/v1/search/sites
POST /api/v1/search
```

一次搜索多个来源：

```json
POST /api/v1/search
{
  "keyword": "artist name",
  "sites": ["danbooru", "x", "pixiv", "eh"],
  "limit": 20,
  "proxy_mode": "required"
}
```

响应按照 `sites` 的顺序返回 `sources[]`。每个来源的 `addresses[]` 是默认展示、供用户
选择的图库地址，包含身份已验证地址和 EH 站内搜索候选；`weak_evidence[]` 保存
X/Pixiv 身份未匹配的次级候选。条目带有 `confidence` 与 `evidence_reasons`：

- X/Twitter：有搜索作品证据，且账号名或显示名与关键词精确匹配的
  `https://x.com/USER/media`；
- Pixiv：有搜索作品证据，且账号名或显示名与关键词精确匹配的
  `https://www.pixiv.net/users/ID/artworks`；
- Danbooru：画师目录主名称或帖子 artist tag 与关键词精确匹配时返回已验证 artist
  地址；仅别名匹配且不存在主名称精确命中时保留为弱证据，避免冲突别名直接升级；
  character tag 仅在角色名本身与关键词精确匹配时返回，不再扩散帖子里的其他角色；
- Danbooru `artist_urls` 能确定的 X/Pixiv 账号属于已验证地址；
- X/Pixiv 标签搜索中身份未匹配的投稿账号进入 `weak_evidence[]`；
- EH 保留通用 `site_search`，命中的全部 `/g/GID/TOKEN/` 画廊直接进入
  `addresses[]`，并通过 gdata API 批量附加真实标题、封面、页数和标签；WebUI 将这些
  标签随每个画廊候选直接展示，由用户选择。

EH 来源同时返回 `tag_facets[]`。分组遵循 [EHWiki Namespace](https://ehwiki.org/wiki/Namespace)
列出的 `artist`、`character`、`cosplayer`、`female`、`group`、`language`、`location`、
`male`、`mixed`、`other`、`parody`、`reclass`，并保留特殊的 `temp` 与未知前缀兜底。
WebUI 同一 namespace 内的多个“包含”标签按 OR 匹配，不同 namespace 之间按 AND 匹配；
任一“排除”标签命中时直接隐藏该画廊。过滤只改变当前显示和“全选当前显示”的范围，
已经勾选的隐藏画廊仍保留选择，并在已选计数中提示。

`limit` 控制每个来源用于匹配的作品/帖子/画廊证据数量。顶层 `address_count` 统计
默认可选地址，`weak_evidence_count` 单独统计弱证据。WebUI 默认展示所有
`addresses[]`；勾选“显示弱证据”后可以继续核对、选择和排序 X/Pixiv 次级候选及
Danbooru 仅别名命中的画师候选。

搜索得到的单条推文、Pixiv 作品和 Danbooru post 只用于判断账号或标签是否匹配，
不会作为最终选择地址返回。某个来源的搜索失败会记录在该来源的 `error` 字段，其他
来源结果仍按原顺序返回。

Danbooru 画师匹配还会读取其人工维护的 `artist_urls`：

- 全部活动平台主页进入顶层 `related_profiles`，同时挂到对应 artist tag；
- 能确定账号的 X/Pixiv URL 会生成 `origin: "danbooru_artist_url"` 的已验证图库地址；
- 已由 X/Pixiv 搜索发现的同一账号会合并来源证据；若原条目位于
  `weak_evidence[]`，则提升到 `addresses[]` 并保留两类证据原因。

X、Pixiv、EH 的授权材料均由授权中心生成并自动注入。`source_options` 的常规用途只剩
各来源的 `proxy_mode`、超时和搜索参数覆盖；兼容脚本仍可显式传入文件型凭据。

### 按选择顺序高速爬取

前端或 API 调用者从搜索响应中选择地址，再按期望顺序提交。后端只有一种批次执行
语义：来源按 `sources[]` 顺序，来源内部按 `addresses[]` 顺序，当前地址内部按图片
并发。

```json
POST /api/v1/crawls
{
  "sources": [
    {
      "site": "danbooru",
      "addresses": [
        {
          "address_type": "artist_tag",
          "label": "artist name",
          "url": "https://danbooru.donmai.us/posts?tags=artist_name"
        },
        {
          "address_type": "character_tag",
          "label": "character name",
          "url": "https://danbooru.donmai.us/posts?tags=character_name"
        }
      ]
    },
    {
      "site": "pixiv",
      "addresses": [
        {
          "address_type": "account",
          "label": "Artist",
          "url": "https://www.pixiv.net/users/123456/artworks"
        }
      ]
    },
    {
      "site": "twitter",
      "addresses": [
        {
          "address_type": "account",
          "label": "@artist",
          "url": "https://x.com/artist/media"
        }
      ]
    },
    {
      "site": "exhentai",
      "addresses": [
        {
          "address_type": "gallery",
          "label": "Selected gallery",
          "url": "https://e-hentai.org/g/GALLERY_ID/TOKEN/"
        }
      ]
    }
  ],
  "concurrency": 20,
  "max_tasks": 10000,
  "proxy_mode": "required"
}
```

执行顺序：

```text
Danbooru artist tag：内部图片最多 20 并发
→ Danbooru character tag：内部图片最多 20 并发
→ Pixiv 账号：内部图片最多 20 并发
→ X 账号：内部图片最多 20 并发
→ EH 画廊：内部图片最多 20 并发
```

当前地址结束后才会创建下一个地址的图片任务。每张图片是独立持久化任务，分别从
代理池取得节点；每个新地址开始规划前，后端先对该地址所属图站执行全池 HTTPS 探活，
规划请求及随后生成的图片任务只使用本次通过的节点。探活结果按地址持久化，服务重启后
仍保持相同筛选范围；切换到下一个地址时重新探活。代理故障时按站点策略冷却并轮换。
X 账号枚举会复用同一次规划得到的 `pbs.twimg.com` / `video.twimg.com` 媒体直链，每个直链
直接建立下载任务，避免为每张图重复请求 X 状态页 API；直链信息不完整时保留状态页回退。
Pixiv 规划只保留每个作品的首个协议 URL，真实页数仍由作品元数据读取，避免多页作品在
规划 JSON 中重复写入全部图片元数据。元数据进程超时后的管道清理也有独立时限。
`concurrency` 受全局调度上限
二次约束，项目默认值为 20。`max_tasks` 是整个批次的媒体任务规模上限。
规划器会额外探测一个作品/帖子，超过上限时将该地址标记为规划错误，以免把账号或
标签图库静默截断；提高 `max_tasks` 后可重新提交完整批次。

批次接口：

```text
POST /api/v1/crawls
GET  /api/v1/crawls
GET  /api/v1/crawls/{batch_id}
GET  /api/v1/crawls/{batch_id}/tasks
POST /api/v1/crawls/{batch_id}/cancel
```

批次、来源顺序、地址顺序和地址到图片任务的映射均保存在 SQLite。服务重启后，
处于规划阶段的地址会重新规划；任务幂等键保证已建立的图片任务被复用。

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
  "max_concurrency": 20,
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

## 托管授权与兼容凭据

默认工作流使用 `/api/v1/auth` 的托管授权。后端在创建搜索、单任务或顺序批次时，按照站点
自动附加托管 Cookie 和专用 gallery-dl cache；X 媒体 CDN 直链任务不附加 Cookie。WebUI
无需提交凭据路径。首次启用时还会将
当前 Windows 用户 gallery-dl 全局缓存中的 Pixiv 登录复制到项目托管缓存，迁移过程不输出
Token。

API 仍兼容 `cookies_file`、`config_file` 和 `credentials_ref`，供脚本调用者显式覆盖；文件路径
必须位于配置的白名单目录。

`credentials_ref: "danbooru_main"` 对应环境变量：

```powershell
$env:GDL_CREDENTIAL_DANBOORU_MAIN_USERNAME = "USER"
$env:GDL_CREDENTIAL_DANBOORU_MAIN_PASSWORD = "API_KEY"
```

兼容凭据通过子进程环境注入，在 worker 内部追加到 gallery-dl 参数。托管 Cookie 位于
`credentials/managed/`，X/Pixiv/EH 共用的 Chrome 配置位于 `browser-profiles/shared/`，
Pixiv Token 位于后端专用 SQLite cache。

## 失败处理

- ProxyError、CONNECT tunnel、407、TLS/SSL 握手和连接拒绝：节点标记失败并进入冷却；
- 429、502、503、504：站点级临时错误，重试但不处罚节点；
- 旧式 X 状态页任务关闭 Cookie 回写，避免多个 worker 争写同一托管 Cookie 文件；
- 登录错误会使对应项目托管凭证进入待重新授权状态，并暂停后续同站排队任务；当前失败任务进入终态；
- 输入错误、不支持 URL、资源不存在：直接进入终态；
- 公共 EH 画廊在特定出口返回 `Insufficient privileges`：冷却该出口并换节点重试；
- gallery-dl 下载 I/O 错误：按任务策略重试；
- 后端重启：校验进程 marker，回收遗留 worker；尚有次数的任务重新排队；
- 用户取消：先发送进程组中断，超时后终止整个子进程树。

## 测试

```bash
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
