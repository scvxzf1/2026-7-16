# Mihomo 安装

后端使用 Mihomo 将隧道节点转换为本地 HTTP 出口。仓库不携带可执行文件，推荐使用项目
脚本安装固定版本 `v1.19.28`。

## 自动安装

Linux：

```bash
bash scripts/install_mihomo.sh
```

Windows PowerShell：

```powershell
.\scripts\install_mihomo.ps1
```

脚本会识别 `amd64`、`arm64` 或 `386`，校验下载归档，解压后执行版本检查，并打印最终
可执行文件的 SHA-256。默认目标：

- Linux：`bin/proxy-core`
- Windows：`bin/proxy-core.exe`

覆盖已有文件或安装到其他目录：

```bash
bash scripts/install_mihomo.sh --force --install-dir /absolute/path
```

```powershell
.\scripts\install_mihomo.ps1 -Force -InstallDir C:\absolute\path
```

## 手动安装

从 [Mihomo v1.19.28 release](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.28)
下载匹配平台和架构的归档，并在解压前核验：

| 平台 | 架构 | 资产 | 归档 SHA-256 |
| --- | --- | --- | --- |
| Linux | amd64/x86_64 | `mihomo-linux-amd64-compatible-v1.19.28.gz` | `70d01cfb8cb7bf7a92fd1af16cb4b9553d90bb4eecde3b5c4849103e27c80ddb` |
| Linux | arm64/aarch64 | `mihomo-linux-arm64-v1.19.28.gz` | `2474450cd1c41dfa53036a54a4e85579f493d3af524d86c3d4b8e2b240b56cd2` |
| Linux | 386 | `mihomo-linux-386-v1.19.28.gz` | `d1d3136bf4a8268bd3c182be976ad10747b1be5f74529ee894434742960915fe` |
| Windows | amd64/x86_64 | `mihomo-windows-amd64-compatible-v1.19.28.zip` | `6d8a079d01b3631e73e56b7b42a067afc14f9e3ad99f2880d38bb141cf8fcbe7` |
| Windows | arm64 | `mihomo-windows-arm64-v1.19.28.zip` | `25cedfb999864e834a3d8424cb8ea61b9145b3cb3aea0180b9fdc009623abeda` |
| Windows | 386/x86 | `mihomo-windows-386-v1.19.28.zip` | `1cc14bdde317b38b861569c1d2aaacaf49907c1707b5aa38838e549b451549b1` |

Linux 将解压后的文件保存为 `bin/proxy-core` 并执行 `chmod 0755 bin/proxy-core`；Windows
将归档中的唯一 `.exe` 保存为 `bin/proxy-core.exe`。最后运行 `-v` 确认文件可执行。

表中摘要属于 release **归档**。配置项 `proxy.transport_core_sha256` 校验的是解压后的
**可执行文件**；自动安装脚本会打印这个值。

## 自定义核心

未显式配置时，后端依次查找项目 `bin/` 和系统 `PATH` 中的
`mihomo`、`verge-mihomo` 或 `proxy-core`。自定义路径使用：

```json
{
  "proxy": {
    "transport_core_binary": "/absolute/path/to/mihomo",
    "transport_core_sha256": "SHA256_OF_EXECUTABLE"
  }
}
```

自定义文件仍会在核心启动前执行配置校验和版本调用。
