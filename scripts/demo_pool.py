#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal demo: load local proxies and pick one via ProxyRotator."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proxy_pool import ProxyRotator, load_proxy_lines, mask_proxy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="代理池轮换演示")
    parser.add_argument(
        "--file",
        default=str(ROOT / "data" / "proxies.txt"),
        help="代理列表文件",
    )
    parser.add_argument("-n", type=int, default=5, help="连续选取次数")
    args = parser.parse_args()

    path = os.path.abspath(args.file)
    pool = load_proxy_lines(path)
    print(f"loaded={len(pool)} from {path}")
    if not pool:
        print("empty pool; put lines into data/proxies.txt")
        return 1

    rot = ProxyRotator(pool)
    for i in range(max(1, args.n)):
        p = rot.next()
        print(f"[{i+1}] {mask_proxy(p) if p else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
