# 独立代理池参考模块

`Proxy_pool/` 是从早期项目抽出的独立代理能力包。主项目
`gallery-dl-backend/` 使用自己的代理实现，两者没有运行时依赖。

## 模块

| 文件 | 职责 |
| --- | --- |
| `proxy_pool.py` | 规范化、轮换、冷却、租约与统计 |
| `proxy_subscription.py` | 导入 HTTP/SOCKS/VLESS/Hy2/Trojan、Clash YAML 等订阅 |
| `local_proxy_forwarder.py` | 将带认证 HTTP 上游包装为本地无认证端口 |
| `embedded_proxy_manager.py` | 管理单个 Mihomo 进程、多端口出口、预检与节点租约 |
| `cross_process_lock.py` | 跨进程文件锁与原子写 |
| `local_paths.py` | 管理本地状态目录 |

## 安装

```bash
cd Proxy_pool
python -m pip install -r requirements.txt
```

使用内嵌传输核心时，系统还需提供 `mihomo` 或 `verge-mihomo` 可执行文件。

## 基本用法

```python
from proxy_pool import ProxyRotator, load_proxy_lines
from proxy_subscription import import_proxy_subscriptions

pool = load_proxy_lines("data/proxies.txt")
rotator = ProxyRotator(pool)

lease = rotator.acquire_lease(owner="worker-0", ttl_seconds=120)
if lease is None:
    raise RuntimeError("当前没有可租用的代理")

success = False
try:
    proxy = lease.proxy
    # 使用 proxy 执行任务
    success = True
finally:
    rotator.release_lease(lease, success=success)

result = import_proxy_subscriptions(["https://SUBSCRIPTION_URL"])
print(result.usable_pool_lines)
```

批量探活、增量订阅、SQLite 统计和 asyncio API 均由模块公开函数提供；具体参数以
函数签名和测试用例为准。

## 本地数据

默认状态目录为包内 `.local/`，可通过 `XAI_LOCAL_DIR` 覆盖：

- 代理统计：`.local/state/proxy_stats.log`
- Mihomo 运行时：`.local/state/embedded_mihomo/`

## 测试

```bash
cd Proxy_pool
python -m unittest discover -s tests -v
```

旧宿主应用的 `webui/http_batch` 不在本目录中，依赖这些模块的集成测试会自动跳过。
内嵌 Mihomo 的设计取舍和当前实现范围见
[`docs/2026-07-12-embedded-mihomo-node-pool-design.md`](./docs/2026-07-12-embedded-mihomo-node-pool-design.md)。
