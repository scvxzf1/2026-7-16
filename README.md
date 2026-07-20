# gallery-dl 数据管理后端

本仓库包含 gallery-dl 调度后端及两个保持独立边界的依赖/参考模块。

| 路径 | 用途 |
| --- | --- |
| [`gallery-dl-backend/`](./gallery-dl-backend/) | FastAPI 后端、跨来源搜索、顺序批次、图片任务调度与代理池 |
| [`gallery-dl-codeberg/`](./gallery-dl-codeberg/) | 上游 gallery-dl Git submodule，源码由上游仓库维护 |
| [`Proxy_pool/`](./Proxy_pool/) | 早期抽出的独立代理池参考模块 |

平台支持：**Windows 与 Linux 完整支持，macOS 兼容预览**。平台限制和运行要求见
[`gallery-dl-backend/README.md`](./gallery-dl-backend/README.md#平台支持)。

## 快速开始

首次检出先初始化上游子模块：

```bash
git submodule update --init --recursive
```

Linux：

```bash
cd gallery-dl-backend
python3 -m venv .venv
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

启动后打开 `http://127.0.0.1:8787/ui/`。配置、授权、API 和测试说明集中在
[`gallery-dl-backend/README.md`](./gallery-dl-backend/README.md)，架构与状态机见
[`gallery-dl-backend/docs/ARCHITECTURE.md`](./gallery-dl-backend/docs/ARCHITECTURE.md)。
