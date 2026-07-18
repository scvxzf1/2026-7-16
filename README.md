# gallery-dl 数据管理后端

本仓库统一保存 gallery-dl 调度后端、代理传输层及其独立参考模块：

| 路径 | 用途 |
|---|---|
| [`gallery-dl-backend/`](./gallery-dl-backend/) | FastAPI 任务后端、跨来源图库地址搜索、顺序批次、单地址图片并发、代理池及内置传输核心管理 |
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

统一接口先按关键词返回经过身份核验的 X/Pixiv 账号、Danbooru artist/character tag，
并补充 Danbooru 人工维护的画师其他平台主页。Pixiv/X 标签搜索中的身份未匹配作者及
Danbooru 仅别名命中的画师单独放入弱证据区；EH 通用站内搜索命中的画廊全部作为可选候选，并批量补齐标题、
封面、页数和标签。WebUI 按 E-Hentai 官方 namespace 分组，支持包含与排除标签快速过滤候选。
用户选择后，来源与来源内地址均按提交顺序执行；当前地址的图片任务最多 20 并发，
每个任务可使用内置 Mihomo 订阅节点或原生 HTTP/HTTPS/SOCKS 节点。

启动后可直接打开 `http://127.0.0.1:8787/ui/` 使用聚合爬取测试台：搜索候选来源、
勾选并调整来源/地址顺序、提交 20 并发批次并查看图片任务状态。该界面为后端内置
静态资源，不包含独立前端构建流程。站点授权由后端统一托管：X/EH 使用各自的项目专属
Chrome 登录窗口并持久化凭证，实际失效后再提示重新授权；Pixiv 通过登录授权写入托管缓存，
Danbooru 公共抓取无需凭据文件。
