# gallery-dl 数据管理后端

本仓库统一保存 gallery-dl 调度后端、代理传输层及其独立参考模块：

| 路径 | 用途 |
|---|---|
| [`gallery-dl-backend/`](./gallery-dl-backend/) | FastAPI 任务后端、跨来源图库地址搜索、顺序批次、单地址图片并发、代理池及传输核心生命周期管理 |
| [`gallery-dl-codeberg/`](./gallery-dl-codeberg/) | 上游 gallery-dl 依赖，作为 Git submodule 保持源码边界 |
| [`Proxy_pool/`](./Proxy_pool/) | 早期独立代理池参考模块 |

## 平台支持

当前对外支持等级为：**Windows 与 Linux 完整支持，macOS 兼容预览**。

| 平台 | 支持等级 | 说明 |
|---|---|---|
| Windows | 完整支持 | 当前主要开发与端到端验证平台；提供 Mihomo 自动下载脚本和 PowerShell 启动脚本。 |
| Linux | 完整支持 | 面向带桌面环境的 Linux；已在 Ubuntu 24.04/Python 3.12 完成全量测试、API/UI、Chrome 授权窗口及 Mihomo 生命周期验证，并提供 Mihomo 自动下载脚本。 |
| macOS | 兼容预览 | 具备 POSIX 基础实现，也需要自行配置代理核心和 Chrome 路径。当前陈旧进程识别仍依赖 Linux `/proc`，重启后的孤儿任务清理尚待适配。 |

更完整的运行说明见 [`gallery-dl-backend/README.md`](./gallery-dl-backend/README.md#平台支持)。

仓库不携带 Windows 或 Linux 的 Mihomo 可执行文件。自动安装脚本固定下载官方
`v1.19.28` release，并在解压前核验对应归档的 SHA-256：

```bash
cd ./gallery-dl-backend
bash ./scripts/install_mihomo.sh
```

```powershell
cd .\gallery-dl-backend
.\scripts\install_mihomo.ps1
```

默认安装位置分别是 `gallery-dl-backend/bin/proxy-core` 和
`gallery-dl-backend/bin/proxy-core.exe`，后端会自动发现，并兼容系统中已有的
`mihomo(.exe)`。手动安装时，从
[Mihomo v1.19.28 release](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.28)
下载与系统架构匹配的 `.gz` 或 `.zip`，按
[`gallery-dl-backend/README.md` 的摘要表](./gallery-dl-backend/README.md#手动安装-mihomo)
核验归档，解压并重命名到上述默认位置；Linux 还需执行
`chmod 0755 ./gallery-dl-backend/bin/proxy-core`。
也可以把 `mihomo` 安装到 `PATH`，或设置 `proxy.transport_core_binary` 为绝对路径；
如需每次启动前固定校验该文件，再填写 `proxy.transport_core_sha256`。

Linux 首次检出：

```bash
git submodule update --init --recursive
cd ./gallery-dl-backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash ./scripts/install_mihomo.sh
cp config.example.json config.json
.venv/bin/python -m unittest discover -s tests -v
bash ./run_backend.sh
```

Windows PowerShell 首次检出：

```powershell
git submodule update --init --recursive
cd .\gallery-dl-backend
python -m pip install -r requirements.txt
.\scripts\install_mihomo.ps1
Copy-Item config.example.json config.json
python -m unittest discover -s tests -v
```

当前后端通过相邻路径 `../gallery-dl-codeberg` 调用 gallery-dl，上游源码仍由
其独立仓库维护。

统一接口先按关键词返回经过身份核验的 X/Pixiv 账号、Danbooru artist/character tag，
并补充 Danbooru 人工维护的画师其他平台主页。Pixiv/X 标签搜索中的身份未匹配作者及
Danbooru 仅别名命中的画师单独放入弱证据区；EH 通用站内搜索命中的画廊全部作为可选候选，并批量补齐标题、
封面、页数和标签。WebUI 按 E-Hentai 官方 namespace 分组，支持包含与排除标签快速过滤候选。
用户选择后，来源与来源内地址均按提交顺序执行；当前地址的图片任务最多 20 并发，
每个任务可使用 Mihomo 托管的订阅节点或原生 HTTP/HTTPS/SOCKS 节点。

启动后可直接打开 `http://127.0.0.1:8787/ui/` 使用聚合爬取测试台：搜索候选来源、
勾选并调整来源/地址顺序、提交 20 并发批次并查看图片任务状态。该界面为后端内置
静态资源，不包含独立前端构建流程。站点授权由后端统一托管：X/EH 使用各自的项目专属
Chrome 登录窗口并持久化凭证，实际失效后再提示重新授权；Pixiv 通过登录授权写入托管缓存，
Danbooru 公共抓取无需凭据文件。
