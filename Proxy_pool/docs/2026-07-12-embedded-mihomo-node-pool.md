# Embedded Mihomo Node Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本项目内嵌 mihomo 兼容层，把订阅 VLESS 节点变成可按任务分配的本地 HTTP 出口，支持预检、复用与失败换节点。

**Architecture:** 新增 `embedded_proxy_manager.py` 管理单 mihomo 进程与多本地 HTTP 口；`proxy_subscription.py` 继续解析订阅；`http_batch_service.py` 在批次启动时 ensure 内核并为每个 worker 分配 `--proxy http://127.0.0.1:port`；WebUI 配置中心提供开关/状态/预检/重载。任务级粘滞，节点不足可复用 healthy 节点，失败最多换 3 个节点。

**Tech Stack:** Python 3、现有 FastAPI WebUI、本机 `mihomo`/`verge-mihomo` 子进程、unittest、现有 `proxy_subscription` 解析。

## Global Constraints

- 第一版只支持 mihomo/verge-mihomo，不接 xray
- 只监听 `127.0.0.1`
- 单任务最多尝试 3 个节点（`embedded_proxy_max_node_retries=3`）
- 预检目标默认 `accounts.x.ai:443`
- 只从 healthy 节点分配；healthy 为空时拒绝分配
- 节点不足允许复用：优先 `ref_count==0`，否则最低 `ref_count`
- 批次运行中禁止重载/停止内嵌内核
- 关闭内嵌时，现有 `none/direct/pool` 行为不变
- 不把 VLESS 直接写入 `curl_cffi` 代理参数
- 测试优先：`python3 -m pytest tests/test_embedded_proxy_manager.py tests/test_http_batch_service.py tests/test_webui_app.py -q`
- UI/文档中文，commit message 英文祈使句

---

## File Map

| 文件 | 职责 |
|---|---|
| `embedded_proxy_manager.py` | 节点模型、配置生成、启停 mihomo、预检、租约 acquire/release、状态导出 |
| `proxy_subscription.py` | 继续拉订阅；必要时补充 VLESS query 字段解析给 mihomo 配置 |
| `http_batch_service.py` | 配置字段、ensure 内核、worker 分配/换节点、运行中重载拒绝 |
| `webui_app.py` | 状态/预检/重载 API |
| `webui/templates/config.html` | 内嵌代理配置 UI |
| `webui/static/config.js` | 字段读写与按钮 |
| `config.example.json` | 示例字段 |
| `tests/test_embedded_proxy_manager.py` | 管理器单测 |
| `tests/test_http_batch_service.py` | 批次接入单测 |
| `tests/test_webui_app.py` | API/页面单测 |
| `docs/superpowers/specs/2026-07-12-embedded-mihomo-node-pool-design.md` | 已通过设计，实现时对照 |

---

### Task 1: 节点模型 + 租约调度（不启进程）

**Files:**
- Create: `embedded_proxy_manager.py`
- Test: `tests/test_embedded_proxy_manager.py`

**Interfaces:**
- Produces:
  - `@dataclass NodeSlot`
  - `@dataclass EmbeddedProxyConfig`
  - `class EmbeddedProxyManager`
  - `manager.acquire(exclude_ids: Optional[set[str]] = None) -> NodeSlot`
  - `manager.release(node_id: str, *, failed: bool = False) -> None`
  - `manager.status() -> dict`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_embedded_proxy_manager.py
import time
import unittest

from embedded_proxy_manager import EmbeddedProxyManager, NodeSlot


class LeaseTests(unittest.TestCase):
    def _mgr(self, n=3):
        m = EmbeddedProxyManager.__new__(EmbeddedProxyManager)
        m._lock = __import__("threading").RLock()
        m._nodes = {}
        for i in range(n):
            slot = NodeSlot(
                id=f"n{i}",
                name=f"node-{i}",
                server=f"{i}.example",
                port=443,
                protocol="vless",
                local_http=f"http://127.0.0.1:{28000+i}",
                healthy=True,
            )
            m._nodes[slot.id] = slot
        m._running = True
        return m

    def test_prefer_idle_then_reuse_lowest_ref(self):
        m = self._mgr(2)
        a = m.acquire()
        self.assertEqual(a.ref_count, 1)
        b = m.acquire()
        self.assertNotEqual(a.id, b.id)
        c = m.acquire()  # reuse
        self.assertIn(c.id, {a.id, b.id})
        self.assertEqual(m._nodes[c.id].ref_count, 2)

    def test_exclude_and_unhealthy_not_selected(self):
        m = self._mgr(2)
        m._nodes["n0"].healthy = False
        got = m.acquire(exclude_ids={"n1"})
        self.assertIsNone(got)

    def test_release_failed_marks_unhealthy_or_cooldown(self):
        m = self._mgr(1)
        n = m.acquire()
        m.release(n.id, failed=True)
        self.assertEqual(m._nodes[n.id].ref_count, 0)
        self.assertTrue(
            (not m._nodes[n.id].healthy)
            or m._nodes[n.id].cooldown_until > time.time()
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_embedded_proxy_manager.py::LeaseTests -q`  
Expected: FAIL（模块/类不存在）

- [ ] **Step 3: 最小实现**

在 `embedded_proxy_manager.py` 实现：

```python
@dataclass
class NodeSlot:
    id: str
    name: str
    server: str
    port: int
    protocol: str
    local_http: str
    raw: str = ""
    params: Dict[str, str] = field(default_factory=dict)
    uuid: str = ""
    healthy: bool = False
    ref_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    last_latency_ms: Optional[float] = None
    cooldown_until: float = 0.0
    last_error: str = ""

class EmbeddedProxyManager:
    def acquire(self, exclude_ids=None) -> Optional[NodeSlot]:
        # healthy and now>=cooldown, not in exclude
        # sort: ref_count asc, last_latency_ms asc (None last)
        # selected.ref_count += 1; return copy/snapshot or same object with current fields

    def release(self, node_id: str, *, failed: bool = False) -> None:
        # ref_count = max(0, ref_count-1)
        # if failed: fail_count+=1; healthy=False; cooldown_until=now+30

    def status(self) -> dict:
        # running, total, healthy, leases, nodes sample
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_embedded_proxy_manager.py::LeaseTests -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add embedded_proxy_manager.py tests/test_embedded_proxy_manager.py
git commit -m "feat: add embedded proxy node lease scheduler"
```

---

### Task 2: VLESS → mihomo 配置生成

**Files:**
- Modify: `embedded_proxy_manager.py`
- Modify: `proxy_subscription.py`（若需把 vless query/uuid 结构化）
- Test: `tests/test_embedded_proxy_manager.py`

**Interfaces:**
- Produces:
  - `parse_vless_node(raw: str) -> Optional[dict]`
  - `build_mihomo_config(nodes: List[NodeSlot], *, listen_host: str, base_port: int) -> dict`
  - `render_mihomo_yaml(config: dict) -> str`
  - 每个 node 的 `local_http = f"http://{host}:{base_port+i}"`

- [ ] **Step 1: 写失败测试**

```python
class ConfigGenTests(unittest.TestCase):
    def test_build_mihomo_config_maps_ports_and_proxies(self):
        from embedded_proxy_manager import NodeSlot, build_mihomo_config
        nodes = [
            NodeSlot(
                id="a", name="jp", server="jp.example", port=443, protocol="vless",
                local_http="", uuid="11111111-1111-1111-1111-111111111111",
                params={"security": "tls", "sni": "jp.example", "type": "tcp"},
            ),
            NodeSlot(
                id="b", name="hk", server="hk.example", port=443, protocol="vless",
                local_http="", uuid="22222222-2222-2222-2222-222222222222",
                params={"security": "reality", "sni": "www.example.com", "pbk": "PK", "sid": "abcd", "type": "tcp", "fp": "chrome"},
            ),
        ]
        cfg = build_mihomo_config(nodes, listen_host="127.0.0.1", base_port=28000)
        self.assertEqual(cfg["allow-lan"], False)
        self.assertEqual(len(cfg["proxies"]), 2)
        self.assertEqual(len(cfg["listeners"]), 2)
        self.assertEqual(cfg["listeners"][0]["port"], 28000)
        self.assertEqual(cfg["listeners"][1]["port"], 28001)
        # 每个 listener 绑定对应 proxy
        self.assertEqual(cfg["listeners"][0]["proxy"], cfg["proxies"][0]["name"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_embedded_proxy_manager.py::ConfigGenTests -q`  
Expected: FAIL

- [ ] **Step 3: 实现配置生成**

要点：

- 使用 mihomo `listeners` 中的 `type: http`（或当前 Meta 支持的 HTTP inbound 写法）为每个节点开本地口
- `proxies[]` 写 vless 字段：`server/port/uuid/tls/reality-opts/network/ws-opts/...`
- 不认识的节点跳过并记 warning，不让整个配置失败
- `build_mihomo_config` 同步回填 `node.local_http`

若 `listeners` 在目标 mihomo 版本不可用，退化为：

- 生成标准 `mixed-port` 单入口配置 **不算完成本任务**
- 必须保持多端口映射；实现前用本机  
  `verge-mihomo -t -f /tmp/test.yaml` 校验一份样例配置

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_embedded_proxy_manager.py::ConfigGenTests -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add embedded_proxy_manager.py proxy_subscription.py tests/test_embedded_proxy_manager.py
git commit -m "feat: generate multi-port mihomo config from vless nodes"
```

---

### Task 3: 二进制探测 + 启停 mihomo + 预检

**Files:**
- Modify: `embedded_proxy_manager.py`
- Test: `tests/test_embedded_proxy_manager.py`

**Interfaces:**
- Produces:
  - `find_mihomo_binary(explicit: str = "") -> str`
  - `manager.start(nodes: List[NodeSlot], config: EmbeddedProxyConfig) -> dict`
  - `manager.stop() -> None`
  - `manager.probe_all(max_workers: int = 8) -> dict`
  - `manager.probe_one(node_id: str) -> dict`
  - 预检：经 `node.local_http` CONNECT/HTTPS 访问 `probe_host:probe_port`

- [ ] **Step 1: 写失败测试（mock 子进程与 socket）**

```python
class LifecycleTests(unittest.TestCase):
    def test_find_binary_prefers_explicit_then_path(self):
        from embedded_proxy_manager import find_mihomo_binary
        self.assertEqual(find_mihomo_binary("/usr/bin/verge-mihomo"), "/usr/bin/verge-mihomo")

    def test_probe_marks_healthy(self):
        # manager with 1 node local_http, mock urllib/socket success => healthy True
        ...

    def test_start_writes_config_and_spawns(self):
        # mock subprocess.Popen, ensure config path written and running True
        ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_embedded_proxy_manager.py::LifecycleTests -q`  
Expected: FAIL

- [ ] **Step 3: 实现**

```python
DEFAULT_PROBE_HOST = "accounts.x.ai"
DEFAULT_PROBE_PORT = 443
DEFAULT_BASE_PORT = 28000
DEFAULT_MAX_NODES = 50
DEFAULT_MAX_NODE_RETRIES = 3

def find_mihomo_binary(explicit: str = "") -> str:
    # explicit path if executable
    # else shutil.which("mihomo") or which("verge-mihomo") or common paths

class EmbeddedProxyManager:
    def start(...):
        # runtime_dir = root/.embedded_mihomo
        # write config.yaml
        # Popen([binary, "-f", config, "-d", runtime_dir], stdout/stderr to log)
        # wait port open for first listener (timeout)
        # self._running=True

    def probe_one(...):
        # use local_http as proxy to TCP/HTTPS probe_host:probe_port
        # success => healthy=True, latency; fail => healthy=False, last_error

    def probe_all(...):
        # ThreadPoolExecutor limited concurrency
```

预检实现可用 `urllib.request` + `ProxyHandler({'http': local_http, 'https': local_http})` 访问 `https://accounts.x.ai/`，或纯 socket 经 HTTP CONNECT。超时用 `embedded_proxy_probe_timeout_sec`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_embedded_proxy_manager.py::LifecycleTests -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add embedded_proxy_manager.py tests/test_embedded_proxy_manager.py
git commit -m "feat: start mihomo and probe xai connectivity per node"
```

---

### Task 4: 订阅导入接入内嵌池

**Files:**
- Modify: `http_batch_service.py`
- Modify: `proxy_subscription.py`（如需导出 vless 结构化字段）
- Test: `tests/test_http_batch_service.py` 或 `tests/test_embedded_proxy_manager.py`

**Interfaces:**
- Produces:
  - `BatchService.ensure_embedded_proxy(force_reload: bool=False) -> dict`
  - `BatchService.get_embedded_proxy_status() -> dict`
  - `BatchService.probe_embedded_proxy() -> dict`
  - 从 `proxy_subscription_url` 拉节点，截断到 `embedded_proxy_max_nodes`

- [ ] **Step 1: 写失败测试**

```python
def test_ensure_embedded_proxy_loads_vless_and_starts(self):
    # temp config embedded_proxy_enabled=true, subscription url set
    # mock import_proxy_subscription / manager.start / probe_all
    # assert status running and node count
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_http_batch_service.py -k embedded -q`  
Expected: FAIL

- [ ] **Step 3: 实现 BatchService 封装**

配置字段写入 `build_config_center` / `update_config_center` / `config.example.json`：

- `embedded_proxy_enabled`
- `embedded_proxy_binary`
- `embedded_proxy_listen_host`
- `embedded_proxy_base_port`
- `embedded_proxy_max_nodes`
- `embedded_proxy_probe_host`
- `embedded_proxy_probe_port`
- `embedded_proxy_probe_timeout_sec`
- `embedded_proxy_max_node_retries`

`ensure_embedded_proxy`：

1. 若未启用，返回 `{enabled:false}`
2. 拉订阅/或使用最近导入结果中的 vless 节点
3. `manager.start`
4. `probe_all`
5. 若 healthy==0 抛 `TuiConfigError("内嵌节点预检全失败...")`

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_http_batch_service.py -k embedded -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add http_batch_service.py proxy_subscription.py config.example.json tests/test_http_batch_service.py
git commit -m "feat: wire subscription nodes into embedded mihomo pool"
```

---

### Task 5: 批次任务按节点分配代理

**Files:**
- Modify: `http_batch_service.py`（`BatchRunner._command_for` / worker 生命周期）
- Test: `tests/test_http_batch_service.py`

**Interfaces:**
- Consumes: `EmbeddedProxyManager.acquire/release`
- Produces: worker 命令使用 `--proxy <local_http>`，不再使用普通 proxy_args（当内嵌启用时）

- [ ] **Step 1: 写失败测试**

```python
def test_command_for_uses_acquired_embedded_proxy(self):
    # plan.embedded_proxy_enabled True
    # mock manager.acquire -> NodeSlot(local_http='http://127.0.0.1:28005', name='jp')
    # command = runner._command_for(worker)
    # assert '--proxy' in command and 'http://127.0.0.1:28005' in command
    # assert '--proxy-file' not in command
```

```python
def test_worker_proxy_failure_retries_up_to_three_nodes(self):
    # simulate worker exit indicating proxy failure thrice / twice then success
    # assert acquire called <=3, release called for each attempt
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_http_batch_service.py -k embedded_proxy -q`  
Expected: FAIL

- [ ] **Step 3: 实现**

改动点：

1. `start_run`/`BatchRunner.start`：若启用内嵌，先 `ensure_embedded_proxy()`
2. `_spawn_worker`/`_command_for`：
   - `node = manager.acquire(exclude_ids=worker.tried_node_ids)`
   - 记 `worker.proxy_node_id/node_name/local_http`
   - `command += ['--proxy', node.local_http]`
3. worker 结束：
   - 成功：`release(id, failed=False)`
   - 若判定为代理失败且 `len(tried) < max_node_retries`：`release(failed=True)` 后用新节点重拉起同一逻辑任务（或标记可重试）
4. `stop`/批次完成：确保所有 lease 释放
5. 日志：
   - `[Proxy] 分配节点 #12 name -> http://127.0.0.1:28012 (lease=1)`
   - `[Proxy] 节点失败，切换 ... (2/3)`

代理失败判定（第一版可保守）：

- 日志/退出码中出现 `CONNECT tunnel failed` / `ProxyError` / `Connection refused` / `curl: (56)` / `curl: (7)` 视为可换节点

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_http_batch_service.py -k embedded -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add http_batch_service.py tests/test_http_batch_service.py
git commit -m "feat: assign per-task embedded mihomo proxy endpoints"
```

---

### Task 6: WebUI API + 配置中心页面

**Files:**
- Modify: `webui_app.py`
- Modify: `webui/templates/config.html`
- Modify: `webui/static/config.js`
- Modify: `config.example.json`
- Test: `tests/test_webui_app.py`

**Interfaces:**
- Produces API:
  - `GET /api/embedded-proxy/status`
  - `POST /api/embedded-proxy/start`（ensure + probe）
  - `POST /api/embedded-proxy/probe`
  - `POST /api/embedded-proxy/stop`（仅非运行批次）
  - `POST /api/embedded-proxy/reload`（stop+start，批次运行中 409/400）

- [ ] **Step 1: 写失败测试**

```python
def test_embedded_proxy_status_and_reload_guard(self):
    # enabled config
    # GET status 200
    # mock busy runner => reload returns 400/409 with Chinese error
```

```python
def test_config_page_has_embedded_proxy_fields(self):
    page = client.get('/config')
    assert 'embedded_proxy_enabled' in page.text
    assert 'btnEmbeddedProbe' in page.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_webui_app.py -k embedded -q`  
Expected: FAIL

- [ ] **Step 3: 实现 UI/API**

配置中心新增区块“内嵌代理内核”：

- checkbox `embedded_proxy_enabled`
- input `embedded_proxy_binary`
- number `embedded_proxy_base_port` / `embedded_proxy_max_nodes`
- text `embedded_proxy_probe_host` / `probe_port` / `timeout`
- 按钮：启动/重载、预检、刷新状态
- 状态区：running、healthy/total、leases、last_error

`config.js`：

- `fill/collectFields` 纳入上述字段
- 按钮调用 API 并渲染状态 JSON/摘要

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_webui_app.py -k embedded -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webui_app.py webui/templates/config.html webui/static/config.js config.example.json tests/test_webui_app.py
git commit -m "feat: expose embedded mihomo controls in webui"
```

---

### Task 7: 端到端烟雾与回归

**Files:**
- Test only / minor fixes
- Possibly `README.md` 或 `USAGE.md` 补 10 行用法

- [ ] **Step 1: 全量相关测试**

```bash
python3 -m pytest \
  tests/test_embedded_proxy_manager.py \
  tests/test_proxy_subscription.py \
  tests/test_http_batch_service.py \
  tests/test_webui_app.py -q
```

Expected: 全绿

- [ ] **Step 2: 本机手工烟雾（有 verge-mihomo）**

```bash
# 配置中心启用内嵌 + 填订阅
# POST /api/embedded-proxy/start
# GET /api/embedded-proxy/status  => healthy > 0
# 随机测一个 local_http:
curl -x http://127.0.0.1:28000 -m 8 -I https://accounts.x.ai/ | head
```

Expected: 至少 1 个节点 healthy；curl 经本地口可连通或返回非“代理拒绝”

- [ ] **Step 3: 关闭内嵌回归**

```bash
# embedded_proxy_enabled=false
# 原 direct/pool 模式仍可保存与测试
python3 -m pytest tests/test_http_batch_service.py -k proxy_mode -q
```

Expected: PASS

- [ ] **Step 4: Commit 文档（如有）**

```bash
git add README.md USAGE.md
git commit -m "docs: explain embedded mihomo node pool usage"
```

---

## Spec Coverage Checklist

| Spec 项 | Task |
|---|---|
| 单 mihomo 多端口 | Task 2/3 |
| 任务级粘滞 | Task 5 |
| 失败最多 3 节点 | Task 5 |
| 节点不足复用 | Task 1 |
| 预检 accounts.x.ai:443 | Task 3 |
| healthy 才可选 | Task 1/3/5 |
| WebUI 开关状态预检重载 | Task 6 |
| 运行中禁止重载 | Task 6 |
| 关闭内嵌不影响旧代理 | Task 5/7 |
| 不把 VLESS 塞进 curl_cffi | Task 5（只传 local http） |

## Placeholder / Consistency Scan

- 无 TBD/TODO
- 方法名统一：`acquire` / `release` / `probe_all` / `ensure_embedded_proxy`
- 配置键统一 `embedded_proxy_*`
- 默认端口 `28000`、预检 `accounts.x.ai:443`、重试 `3`

