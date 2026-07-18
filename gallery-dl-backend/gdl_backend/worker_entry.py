from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    args = list(sys.argv[1:])
    try:
        separator = args.index("--")
    except ValueError as exc:
        raise SystemExit("worker_entry requires '--' before gallery-dl arguments") from exc
    control_args = args[:separator]
    gallery_args = args[separator + 1 :]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--gallery-root", required=True)
    control = parser.parse_args(control_args)

    gallery_root = Path(control.gallery_root).resolve()
    if not (gallery_root / "gallery_dl" / "__init__.py").is_file():
        raise SystemExit(f"gallery-dl source not found: {gallery_root}")
    sys.path.insert(0, str(gallery_root))

    username = os.environ.pop("GDL_WORKER_USERNAME", "")
    password = os.environ.pop("GDL_WORKER_PASSWORD", "")
    if username:
        gallery_args[0:0] = ["--username", username]
    if password:
        gallery_args[0:0] = ["--password", password]

    sys.argv = ["gallery-dl", *gallery_args]
    import gallery_dl

    return int(gallery_dl.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
