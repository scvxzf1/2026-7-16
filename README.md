# gallery-dl 数据管理后端

本仓库统一保存 gallery-dl 调度后端、代理传输层及其独立参考模块：

| 路径 | 用途 |
|---|---|
| [`gallery-dl-backend/`](./gallery-dl-backend/) | 当前 FastAPI 任务后端、代理池、内置传输核心管理及 EH 并发脚本 |
| [`gallery-dl-codeberg/`](./gallery-dl-codeberg/) | 上游 gallery-dl 依赖，作为 Git submodule 保持源码边界 |
| [`Proxy_pool/`](./Proxy_pool/) | 早期独立代理池参考模块 |

首次检出：

```powershell
git submodule update --init --recursive
cd .\gallery-dl-backend
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
python -m pytest -q
```

当前后端通过相邻路径 `../gallery-dl-codeberg` 调用 gallery-dl，上游源码仍由
其独立仓库维护。
