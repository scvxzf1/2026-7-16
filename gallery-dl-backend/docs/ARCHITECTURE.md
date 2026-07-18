# 架构与状态机

## 进程边界

FastAPI 与任务调度器运行在主进程。每次下载启动一个：

```text
python -m gdl_backend.worker_entry --marker TASK_TOKEN --gallery-root PATH -- GALLERY_ARGS URL
```

`worker_entry` 从指定源码目录导入 gallery-dl。命令行 marker 用于后端重启时核验 PID，降低 PID 复用导致误终止其他进程的风险。

代理池控制面运行在 FastAPI 主进程内：`proxy_sources` 解析机场订阅，`NativeProxyPool` 管理轮换、原子租约和冷却。带认证的 HTTP 上游在租约期间由 `LocalHTTPForwarder` 暴露为随机本地端口，任务结束时同步关闭。普通 HTTP/SOCKS 代理直接交给 gallery-dl。

Clash YAML 隧道节点由 `TunnelTransportCore` 管理一个项目内核心子进程。后端生成一份最小运行配置，每个订阅节点对应一个仅绑定 `127.0.0.1` 的 HTTP listener，并用 listener 的 `proxy` 字段固定到该出站节点。核心只负责协议传输；调度、探活、租约、重试和冷却仍由 Python 控制面负责。

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
