# 代理池（从 grok 协议项目抽出）

独立可复用的代理能力包，路径：

`/home/scv/nvme0n1p1/构思-成功/大合集/代理池`

## 模块

| 文件 | 职责 |
|------|------|
| `proxy_pool.py` | 规范化 / 校验 / 加权轮换 / 冷却 / 租约 / 统计 |
| `proxy_subscription.py` | 订阅拉取与解析（HTTP/SOCKS/VLESS/Hy2/Trojan/Clash YAML…） |
| `local_proxy_forwarder.py` | 本地无鉴权 HTTP 转发（给浏览器注入上游账号密码） |
| `embedded_proxy_manager.py` | 内嵌 mihomo：节点租约、预检、多端口 HTTP 出口 |
| `cross_process_lock.py` | 跨进程文件锁与原子写 |
| `local_paths.py` | 数据目录（默认 `代理池/.local/`） |

## 安装

```bash
cd /home/scv/nvme0n1p1/构思-成功/大合集/代理池
pip install -r requirements.txt
```

内嵌 mihomo 还需本机有 `mihomo` / `verge-mihomo` 可执行文件。

## 快速用法

```python
from proxy_pool import ProxyRotator, load_proxy_lines, normalize_proxy_pool
from proxy_subscription import import_proxy_subscriptions

# 手动池
pool = load_proxy_lines("data/proxies.txt")
rot = ProxyRotator(pool)
proxy = rot.next()
rot.record_result(proxy, success=True)

# 租约（并发任务互斥）
lease = rot.acquire_lease(owner="worker-0", ttl_seconds=120)
# ... 用 lease.proxy ...
rot.release_lease(lease, success=True)

# 订阅
result = import_proxy_subscriptions(["https://your-sub-url"])
print(result.usable_pool_lines)
print(result.to_dict()["scheme_counts"])
```

本地鉴权转发：

```python
from local_proxy_forwarder import ensure_local_forwarder, stop_local_forwarder

url, used = ensure_local_forwarder(
    "http://user:pass@gate.example:1000",
    preferred_local_port=17890,
    instance_key="worker-0",
)
# browser 用 url；用完后
stop_local_forwarder("worker-0")
```

## 数据目录

默认写到包内 `.local/`（可用环境变量覆盖）：

- `XAI_LOCAL_DIR` → 本地根目录
- 状态/统计：`.local/state/proxy_stats.log`
- 内嵌 mihomo 运行时：`.local/state/embedded_mihomo/`

## 测试

```bash
cd /home/scv/nvme0n1p1/构思-成功/大合集/代理池
python -m unittest discover -s tests -v
```

说明：`test_proxy_subscription` 在原项目里依赖 webui/http_batch，未整包拷入；订阅解析可直接 import `proxy_subscription` 使用。

## 性能优化（P0–P2，已落地）

在 `proxy_pool.py` / `proxy_subscription.py` 上：

| 级别 | 项 | 实现 |
|------|----|------|
| P0 | 状态批量刷盘 | `ProxyRotator`：`_pending_logs` + `dirty` + `save_interval`（默认 2s）+ 后台 flush 线程；`flush(force=True)` / `close()` |
| P0 | 健康检查并发 | `check_proxies_concurrent(..., max_workers=50)` |
| P0 | 订阅并发拉取 | `import_proxy_subscriptions(..., max_workers=8)` 线程池 |
| P1 | 可用集 + 权重缓存 | `_available` set、`_country_weight_cache`，冷却/租约时增量维护 |
| P1 | 订阅间隔 / 只合并新增 | `default_interval_seconds`、`per_url_intervals`、`force`；`fetch_and_merge_new`；状态 `data/source_fetch_state.json` |
| P1 | 连接复用 | 进程级 `urllib` opener |
| P2 | 增量评分 | 国家 success/fail 计数 O(1) 更新 |
| P2 | 字典索引 + 并发 | `_proxy_set` / lease maps + `threading.RLock` |
| P3 | orjson 序列化 | `_json_dumps`：有 orjson 用 orjson，否则 stdlib json |
| P3 | SQLite 统计后端 | `stats_backend="sqlite"` 或 `stats_file` 以 `.db` 结尾；WAL + 聚合表 |
| P3 | asyncio API | `import_proxy_subscriptions_async` / `check_proxies_async` / `fetch_and_merge_new_async` |

```python
from proxy_pool import ProxyRotator
from proxy_subscription import (
    import_proxy_subscriptions,
    fetch_and_merge_new,
    check_proxies_concurrent,
    check_proxies_async,
    import_proxy_subscriptions_async,
)

rot = ProxyRotator(pool, save_interval=2.0)
# 万级规模：SQLite 后端
rot_db = ProxyRotator(pool, stats_file="data/stats.db", stats_backend="sqlite")
# ... mark/record ...
rot.flush(force=True)  # 退出前

result = import_proxy_subscriptions(
    urls,
    max_workers=8,
    default_interval_seconds=300,
    per_url_intervals={"https://tight.example/s": 3600},
)
new_lines = fetch_and_merge_new(urls, existing_pool_lines=pool, force=False)
health = check_proxies_concurrent(new_lines, max_workers=50)

# asyncio 应用内
# result = await import_proxy_subscriptions_async(urls, force=True)
# health = await check_proxies_async(new_lines, concurrency=50)
```

验证：

```bash
python3 -m unittest tests.test_proxy_pool tests.test_proxy_subscription_perf tests.test_p3_storage_async -v
```

## 来源

自 `/home/scv/nvme0n1p1/注册机相关/grok协议-es1/grok协议` 抽出，并在本目录完成 P0–P2 优化与单测。
