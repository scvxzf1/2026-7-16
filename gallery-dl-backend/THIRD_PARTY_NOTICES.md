# Third-party installer notice

This repository does not distribute a Mihomo executable. The optional
`scripts/install_mihomo.ps1` and `scripts/install_mihomo.sh` installers download an
unmodified platform archive from
[MetaCubeX/mihomo v1.19.28](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.28)
only when a user runs the script.

- Each installer pins the upstream release asset and its SHA-256 for amd64,
  arm64, and 386.
- The archive digest is verified before extraction, and the installed executable
  digest and version are printed after installation.
- License: GNU GPL version 3; local copy at `bin/LICENSE.mihomo-GPL-3.0.txt`.
- Corresponding upstream source: <https://github.com/MetaCubeX/mihomo/tree/v1.19.28>.

The Python backend and a user-installed Mihomo executable remain separate
processes and communicate through local HTTP listener ports.
