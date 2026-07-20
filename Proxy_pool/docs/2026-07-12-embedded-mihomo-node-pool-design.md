# 内嵌 Mihomo 多节点池决策记录

- 日期：2026-07-12
- 状态：独立核心已实现；旧宿主集成不在当前模块范围内
- 范围：将订阅中的隧道节点映射为可租用的本地 HTTP 出口

## 背景

HTTP 客户端不能直接消费 VLESS 等分享链接。要求用户另行启动 Clash/V2Ray 还会把
核心生命周期、节点选择和任务重试分散到项目外部。

## 决策

采用“**单个 Mihomo 进程、多本地监听端口、任务级租约**”：

```text
订阅节点
  -> 生成一份 Mihomo 配置
  -> 每个节点绑定一个 127.0.0.1 HTTP listener
  -> 启动并预检节点
  -> 任务取得节点租约并全程使用同一出口
  -> 任务结束后释放租约
```

没有采用每任务一个核心进程，因为进程和端口开销过高；也没有采用单入口动态切换，
因为并发任务会共享并互相改变出口。

## 运行规则

- 监听地址固定为 `127.0.0.1`。
- 只向任务分配最近预检成功且不在冷却期的节点。
- 优先空闲节点；节点不足时选择引用计数最低的节点复用。
- 单个任务保持节点粘性，换节点由宿主在任务重试边界处理。
- Mihomo 只负责协议传输；订阅、探活、租约和统计由 Python 管理器负责。
- 核心二进制由本机提供，不随仓库分发。

## 当前实现

| 文件 | 当前职责 |
| --- | --- |
| `proxy_subscription.py` | 拉取并解析订阅节点 |
| `embedded_proxy_manager.py` | 解析隧道节点、生成配置、启停核心、预检和租约 |
| `tests/test_embedded_proxy_manager.py` | 覆盖租约、配置生成、生命周期和探测 |

独立模块已经实现 VLESS、Hysteria2、AnyTLS、Trojan 等节点解析，以及多 listener
配置和 `EmbeddedProxyManager` 生命周期。运行数据写入 `.local/state/embedded_mihomo/`。

早期方案还规划了 `http_batch_service.py`、WebUI API 和配置页面。这些宿主文件不在
`Proxy_pool/` 中，因此不属于当前独立模块的完成条件；接入方应在自己的任务边界调用
管理器，并负责重试、批次状态和界面。

## 边界

- 不把 VLESS 等 URI 直接传给 HTTP 客户端。
- 不在本模块内维护 xray 后端、跨机器调度或 WebUI。
- 不承诺运行中无损热更新；宿主应在没有活动租约时重载核心。
- 主项目 `gallery-dl-backend/` 有独立的 `TunnelTransportCore` 实现，不导入本模块。

## 验证

```bash
cd Proxy_pool
python -m unittest tests.test_embedded_proxy_manager -v
```

需要真实核心和节点的联网探测属于本机烟雾测试，单元测试使用本地夹具和 mock。
