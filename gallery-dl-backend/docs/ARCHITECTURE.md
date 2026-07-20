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

代理池控制面运行在 FastAPI 主进程内：`proxy_sources` 先完成全部机场订阅、节点文件和内联节点的解析，再由 `NativeProxyPool` 管理轮换、原子租约和冷却。导入及探活没有节点数量上限。带认证的 HTTP 上游在租约期间由 `LocalHTTPForwarder` 暴露为随机本地端口，任务结束时同步关闭。普通 HTTP/SOCKS 代理直接交给 gallery-dl。

站点授权控制面由 `AuthManager` 管理。X、Pixiv 与 EH 共用
`credentials/managed/browser-profiles/shared/` 下的一个持久 Chrome Profile 和一个运行中宿主进程。
后端以随机且非零的本地 DevTools 端口启动可见浏览器，端口只绑定 `127.0.0.1`；三个站点的授权
流程串行创建独立标签页，完成、取消或超时后只关闭对应 Target。满足 X/EH 必需 Cookie 后原子写入
`credentials/managed/*.cookies.txt`，共享 Profile 中的 Cookie、本地存储和设备历史继续保留。
Pixiv OAuth 由受控 gallery-dl 子进程完成，refresh-token
先写入单次会话的隔离 cache，交换成功后再原子更新后端专用 cache，取消或失败会清理会话 cache；
OAuth callback 监听绑定本次 Pixiv Target，并在页面导航前启用 Network 事件。
FastAPI 只返回授权状态、Cookie 数量、登录会话进度和缺失项，不返回值、配置目录或 DevTools
地址。托管目录在 Windows 上
收紧为当前用户、SYSTEM 与 Administrators，在 POSIX 系统上使用目录 `0700`、文件 `0600`。
Danbooru 公共抓取标记为无需登录。搜索和爬取请求未显式覆盖凭据时，后端按来源自动注入这些
托管文件及 cache；X 枚举后生成的 `pbs.twimg.com` / `video.twimg.com` 媒体直链任务不再携带
账号 Cookie。搜索、EH 规划或下载出现认证错误时，仅与项目托管文件精确匹配的凭据会
写入持久失效标记；调度器跳过仍引用该文件的排队任务，重新登录原子更新 Cookie 和元数据后
下一轮调度自动继续。

单站清理只删除后端导出的 Cookie 或 Pixiv Token。独立的共享 Profile 清理接口会先取消全部授权
会话并关闭 Chrome 宿主，再删除整个 `shared/` 目录；导出凭证仍由站点接口分别管理。

Clash YAML 隧道节点由 `TunnelTransportCore` 管理一个项目内核心子进程。后端生成一份最小运行配置，每个订阅节点对应一个仅绑定 `127.0.0.1` 的 HTTP listener，并用 listener 的 `proxy` 字段固定到该出站节点。核心只负责协议传输；调度、探活、租约、重试和冷却仍由 Python 控制面负责。

## 跨来源发现与选择

`DiscoveryService` 用 gallery-dl DataJob JSON 协议运行元数据子进程，不下载媒体文件：

- Danbooru：artist 主名称或帖子 artist tag 精确匹配时生成已验证标签地址；仅别名匹配
  且不存在主名称精确命中时进入弱证据区，不把冲突别名或采样帖中的其他角色标签提升；
- X/Twitter 与 Pixiv：账号发现不执行站内搜索。请求这些来源时，后端在内部补充一次
  Danbooru 查询，并只采用 `artist_urls` 中可规范化的活动账号；选中后仍由 gallery-dl
  分别枚举 `/media` 或 `/users/{id}/artworks`；
- EH：保留通用站内搜索；Queue 消息形成具体 `/g/GID/TOKEN/` 画廊地址，再以一次或
  多次 gdata 批量请求补齐标题、封面、页数和标签。所有站内命中都进入默认可选地址，
  后端将标签汇总为官方 namespace `tag_facets[]`；WebUI 在候选行内直接展示封面、标题和
  标签，并提供分组包含/排除过滤，由用户结合预览判断。

`POST /api/v1/search` 可以一次查询多个来源，并始终按请求顺序返回 `sources[]`。
响应的 `sources[].addresses[]` 保存默认可选的已验证账号/标签地址与 EH 站内画廊候选，
`sources[].weak_evidence[]` 保存 Danbooru 仅别名命中的画师候选。WebUI 默认展示
`addresses[]`，用户显式打开弱证据后还可核对并提交次级候选。

EH 标签分组遵循 EHWiki 的 `artist`、`character`、`cosplayer`、`female`、`group`、
`language`、`location`、`male`、`mixed`、`other`、`parody`、`reclass` namespace；
`temp` 单独保留，未识别前缀归入 `unknown`。过滤器采用组内 OR、组间 AND、排除优先，
只作用于浏览器当前候选视图，不重新请求站点或删除响应中的原始地址。
Danbooru artist tag 还会查询 `artists.json` 与 `artist_urls.json`，把人工维护的其他
活动平台主页原样返回。可确定为 X 或 Pixiv 账号的 URL 会同时生成已验证图库地址；
其余主页保存在 `related_profiles` 供前端展示。

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

每次地址从 `pending` 进入 `planning` 后，管理器先从地址 URL 提取 HTTPS 站点根地址
（站点策略显式配置 `probe_url` 时优先使用），对全池执行一次探活。通过节点集合与探活
摘要按地址持久化；该地址的发现、索引规划及全部图片任务只能从此集合取得租约。服务重启
继续执行已建立任务时从 SQLite 恢复集合，切换到下一个地址时重新探活。

单地址统一执行图片级规划：

- X 账号：枚举时同时收集 gallery-dl `Message.Url` 给出的媒体 CDN URL，完整时直接为每个
  `pbs.twimg.com` / `video.twimg.com` URL 建立任务；只有直链缺失时才回退到状态页 `--range N`；
- Pixiv 账号：枚举时仅保留每个作品的首个协议 URL，并从 Directory 元数据读取真实页数，
  避免多页作品在规划阶段复制整套文件元数据；多图作品再通过独立 `--range N` 任务拆分；
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
- `crawl_address_proxy_probes`：每个地址最近一次站点探活目标、时间及汇总；
- `crawl_address_proxy_nodes`：每个地址通过站点探活的节点集合。

数据库启用 WAL、foreign_keys、busy_timeout 和 NORMAL synchronous。

## 代理选择

1. 新地址规划前对对应图站执行全池 HTTPS 探活并持久化通过节点；
2. `NativeProxyPool.acquire()` 将候选限制到该地址的通过集合；
3. 原子排除已租用、冷却及任务已尝试节点；
4. 应用站点 `node_tags`；
5. 使用内部轮询游标分配节点，并记录成功、失败和冷却状态；
6. 可选执行任务取用前的二次站点 HTTPS 探活；
7. 任务全程固定同一代理；
8. 仅明确的代理故障处罚节点。

节点 `healthy` 只记录最近一次探活结果；冷却到期后仅将 `retry_eligible` 置为真，不会在没有新探活成功的情况下自动恢复健康状态。默认单节点 HTTPS 探活超时为 10 秒。

## 订阅协议边界

- 直接进入运行池：HTTP、HTTPS、无认证 SOCKS4/SOCKS5/SOCKS5H；
- 带认证 HTTP 由任务级本地转发器承接；带认证 SOCKS5 由传输核心桥接；
- Base64、纯文本、Clash YAML/JSON、sing-box JSON 与 SIP008 均可导入；
- VLESS、VMess、Trojan、Shadowsocks/SSR、Hysteria/Hysteria2、TUIC、AnyTLS、Mieru 进入 `TunnelTransportCore`；
- 每个核心节点映射为一个本地 HTTP endpoint，再与原生代理采用相同的任务租约语义；
- 核心配置写入 `runtime/proxy/transport-core/config.yaml`，日志写入相邻 `core.log`；目录由 `.gitignore` 排除；
- 核心启动前执行配置校验，所有 listener 就绪后才开放代理池。

## gallery-dl 隔离

- 后端从源码路径启动子进程，不调用系统中另一个版本的 gallery-dl；
- 每个任务使用 `--config-ignore`，随后按需加载白名单内的显式配置；
- 输出、代理、Cookie、超时和重试由后端构造；
- 仍需读取 X 状态页的任务强制关闭 gallery-dl Cookie 回写，避免并发更新共享文件；
- `--exec`、输出覆盖、代理覆盖、任意配置注入等参数从 API 层拦截；
- 子进程 stdout/stderr 统一采集到 SQLite。
- 元数据子进程达到输出或时间上限后，管道收尾也有独立时限，残留 reader 会被取消。
