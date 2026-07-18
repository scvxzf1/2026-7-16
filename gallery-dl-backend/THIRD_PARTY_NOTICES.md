# Third-party binary notice

`bin/proxy-core.exe` is an unmodified compatible Windows amd64 build of
[MetaCubeX/mihomo v1.19.28](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.28).

- Upstream release archive: `mihomo-windows-amd64-compatible-v1.19.28.zip`
- Release archive SHA-256: `6d8a079d01b3631e73e56b7b42a067afc14f9e3ad99f2880d38bb141cf8fcbe7`
- Extracted executable SHA-256: `a3799f2d75c623a7c6d307e1faf88269e24dd746c59df3e9f1c84d5cfbff6c92`
- The backend verifies this executable digest before every managed core start.
- License: GNU GPL version 3; local copy at `bin/LICENSE.mihomo-GPL-3.0.txt`
- Corresponding upstream source: <https://github.com/MetaCubeX/mihomo/tree/v1.19.28>

The Python backend and the upstream executable remain separate processes and
communicate through local HTTP listener ports.
