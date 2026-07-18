# 架构与状态机

## 进程边界

FastAPI、`DiscoveryService`、`OrderedCrawlManager`、`CrawlPlanner` 与任务调度器运行在主进程。每个图片任务启动一个：

```text
python -m gdl_backend.worker_entry --marker TASK_TOKEN --gallery-root PATH -- GALLERY_ARGS URL
```

`worker_entry` 从指定源码目录导入 gallery-dl。命令行 marker 用于后端重启时核验 PID，降低 PID 复用导致误终止其他进程的风险。

FastAPI 同源挂载 `/ui/` 下的纯 HTML/CSS/JavaScript 测试台。界面直接调用现有
`/api/v1/search`、`/api/v1/crawls` 与代理池接口，只负责候选选择、顺序调整和状态
展示；搜索归并、顺序约束、代理租约及任务状态仍以后端数据库为准。

代理池控制面运行在 FastAPI 主进程内：`proxy_sources` 解析机场订阅，`NativeProxyPool` 管理轮换、原子租约和冷却。带认证的 HTTP 上游在租约期间由 `LocalHTTPForwarder` 暴露为随机本地端口，任务结束时同步关闭。普通 HTTP/SOCKS 代理直接交给 gallery-dl。

站点授权控制面由 `AuthManager` 管理。X 与 EH 分别使用
`credentials/managed/browser-profiles/{twitter,exhentai}/` 下的项目专属 Chrome 配置。
后端以随机且非零的本地 DevTools 端口启动可见登录窗口，避免 `--remote-debugging-port=0`
使 Chrome 暴露自动化标记；端口只绑定 `127.0.0.1`。重新授权前通过页面 CDP
`Network.deleteCookies` 只删除该站的登录 Cookie，保留 Cloudflare clearance 等浏览器会话状态。
满足必需 Cookie 后原子写入 `credentials/managed/*.cookies.txt` 并关闭窗口；后续启动直接复用持久文件。
Pixiv OAuth 由受控 gallery-dl 子进程完成，refresh-token
先写入单次会话的隔离 cache，交换成功后再原子更新后端专用 cache，取消或失败会清理会话 cache；
FastAPI 只返回授权状态、Cookie 数量、登录会话进度和缺失项，不返回值、配置目录或 DevTools
地址。托管目录在 Windows 上
收紧为当前用户、SYSTEM 与 Administrators，在 POSIX 系统上使用目录 `0700`、文件 `0600`。
Danbooru 公共抓取标记为无需登录。搜索和爬取请求未显式覆盖凭据时，后端按来源自动注入这些
托管文件及 cache。搜索、EH 规划或下载出现认证错误时，仅与项目托管文件精确匹配的凭据会
写入持久失效标记；调度器跳过仍引用该文件的排队任务，重新登录原子更新 Cookie 和元数据后
下一轮调度自动继续。

Clash YAML 隧道节点由 `TunnelTransportCore` 管理一个项目内核心子进程。后端生成一份最小运行配置，每个订阅节点对应一个仅绑定 `127.0.0.1` 的 HTTP listener，并用 listener 的 `proxy` 字段固定到该出站节点。核心只负责协议传输；调度、探活、租约、重试和冷却仍由 Python 控制面负责。

## 跨来源发现与选择

`DiscoveryService` 用 gallery-dl DataJob JSON 协议运行元数据子进程，不下载媒体文件：

- X/Twitter：以搜索作品为证据归并账号的 `/media` 地址；只有账号名或显示名与关键词
  精确匹配的条目直接验证，其余保留为弱证据；
- Pixiv：以标签搜索作品为证据归并 `/users/{id}/artworks`；身份匹配规则与 X 相同；
- Danbooru：artist 主名称或帖子 artist tag 精确匹配时生成已验证标签地址；仅别名匹配
  且不存在主名称精确命中时进入弱证据区，不把冲突别名或采样帖中的其他角色标签提升；
- EH：保留通用站内搜索；Queue 消息形成具体 `/g/GID/TOKEN/` 画廊地址，再以一次或
  多次 gdata 批量请求补齐标题、封面、页数和标签。所有站内命中都进入默认可选地址，
  后端将标签汇总为官方 namespace `tag_facets[]`；WebUI 在候选行内直接展示封面、标题和
  标签，并提供分组包含/排除过滤，由用户结合预览判断。

`POST /api/v1/search` 可以一次查询多个来源，并始终按请求顺序返回 `sources[]`。
响应的 `sources[].addresses[]` 保存默认可选的已验证账号/标签地址与 EH 站内画廊候选，
`sources[].weak_evidence[]` 保存身份尚未闭环的 X/Pixiv 账号候选及 Danbooru 仅别名
命中的画师候选；搜索作品本身只作为匹配证据。WebUI 默认展示 `addresses[]`，用户
显式打开弱证据后还可核对并提交次级候选。

EH 标签分组遵循 EHWiki 的 `artist`、`character`、`cosplayer`、`female`、`group`、
`language`、`location`、`male`、`mixed`、`other`、`parody`、`reclass` namespace；
`temp` 单独保留，未识别前缀归入 `unknown`。过滤器采用组内 OR、组间 AND、排除优先，
只作用于浏览器当前候选视图，不重新请求站点或删除响应中的原始地址。
Danbooru artist tag 还会查询 `artists.json` 与 `artist_urls.json`，把人工维护的其他
活动平台主页原样返回。可确定为 X 或 Pixiv 账号的 URL 会同时生成已验证图库地址；
若同一地址先由站内搜索进入弱证据区，则将其提升并合并证据。其余主页保存在
`related_profiles` 供前端展示。

## 顺序批次与单地址并发

客户端从搜索响应中选择地址后提交 `sources[]`：

```text
来源 0 / 地址 0（内部图片并发）
  → 来源 0 / 地址 1（内部图片并发）
  → 来源 1 / 地址 0（内部图片并发）
  → ...
```

`OrderedCrawlManager` 只为当前地址建立图片任务。当前地址的全部图片任务进入终态后，
才激活同一来源的下一个地址；来源内地址结束后，再激活下一个来源。因此来源顺序和
地址顺序由 SQLite 持久化，不依赖内存列表或协程完成先后。

单地址统一执行图片级规划：

- X/Pixiv 账号：先枚举账号作品，多图作品通过独立 `--range N` 任务拆分；
- Danbooru artist/character tag：枚举标签下 posts，每个图片 post 建立一个任务；
- EH 画廊：读取索引中的 `/s/TOKEN/GID-NUM`，每张图片建立 `--range 1` 任务。

搜索、账号/标签枚举和 EH 索引规划使用短期代理租约；每个图片下载任务再独立获取
一个全程粘性的代理租约。Mihomo 隧道节点与原生 HTTP/HTTPS/SOCKS 节点对上层使用
同一 `ProxyPoolAdapter`。请求的 `concurrency` 是当前单地址图片任务并发上限，并再受
`scheduler.max_concurrent_tasks` 全局上限限制。

## 任务状态

```text
queued → starting → running → succeeded
                          ├→ queued（可重试）
                          ├→ failed
                          └→ cancelling → cancelled
```

每次 `running` 都会生成一条 attempts 记录。代理租约在启动 gallery-dl 前持久化，并在任意结束路径的 `finally` 中释放。

## SQLite 表

- `tasks`：任务状态、站点、输出目录、重试与错误摘要；
- `attempts`：每次执行的 PID、代理节点、退出码和错误分类；
- `leases`：正在使用的节点；
- `task_logs`：脱敏 stdout/stderr/backend 日志；
- `task_events`：状态变化事件；
- `site_policies`：每站策略。
- `crawl_batches`：顺序批次、并发上限和聚合计数；
- `crawl_addresses`：来源顺序、地址顺序、规划状态及来源级凭据；
- `crawl_address_tasks`：地址与图片任务的稳定序号映射。

数据库启用 WAL、foreign_keys、busy_timeout 和 NORMAL synchronous。

## 代理选择

1. `NativeProxyPool.acquire()` 原子排除已租用和冷却节点；
2. 过滤任务已尝试节点；
3. 应用站点 `node_tags`；
4. 使用内部轮询游标分配节点，并记录成功、失败和冷却状态；
5. 可选执行站点 HTTPS 探活；
6. 任务全程固定同一代理；
7. 仅明确的代理故障处罚节点。

## 订阅协议边界

- 直接进入运行池：HTTP、HTTPS、无认证 SOCKS4/SOCKS5/SOCKS5H；
- 带认证 HTTP 由任务级本地转发器承接；带认证 SOCKS 仅进入来源统计；
- Clash YAML 中的 VLESS、Hysteria2、AnyTLS、Trojan、VMess、Shadowsocks、Mieru 进入 `TunnelTransportCore`；
- 每个核心节点映射为一个本地 HTTP endpoint，再与原生代理采用相同的任务租约语义；
- 核心配置写入 `runtime/proxy/transport-core/config.yaml`，日志写入相邻 `core.log`；目录由 `.gitignore` 排除；
- 核心启动前执行配置校验，所有 listener 就绪后才开放代理池。

## gallery-dl 隔离

- 后端从源码路径启动子进程，不调用系统中另一个版本的 gallery-dl；
- 每个任务使用 `--config-ignore`，随后按需加载白名单内的显式配置；
- 输出、代理、Cookie、超时和重试由后端构造；
- `--exec`、输出覆盖、代理覆盖、任意配置注入等参数从 API 层拦截；
- 子进程 stdout/stderr 统一采集到 SQLite。
