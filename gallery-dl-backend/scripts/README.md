# 运维脚本

从 `gallery-dl-backend/` 目录运行本目录中的脚本。

## Mihomo 安装

| 脚本 | 平台 |
| --- | --- |
| `install_mihomo.sh` | Linux |
| `install_mihomo.ps1` | Windows PowerShell |

版本、校验值和自定义安装参数见 [`../docs/MIHOMO.md`](../docs/MIHOMO.md)。

## EH 联网烟雾测试

这两个脚本需要可用的 `config.json`、EH 授权和代理节点，不属于默认单元回归。

`eh_resample_smoke.py` 同时排队 20 个任务：19 个执行搜索/模拟提取，1 个下载首张
resample，用于检查调度并发、代理租约和基础下载链路。

```powershell
python scripts\eh_resample_smoke.py `
  --config config.json `
  --query '"clover days"' `
  --concurrency 20
```

`eh_full_gallery_parallel.py` 搜索首个匹配画廊，为每张图片建立独立任务，并核对缺页、重页、
文件摘要和并发峰值。

```powershell
python scripts\eh_full_gallery_parallel.py `
  --config config.json `
  --query '"clover days"' `
  --concurrency 20 `
  --timeout 1800
```

脚本在独立运行目录生成 `report.json`。其他参数使用 `python SCRIPT --help` 查看。
