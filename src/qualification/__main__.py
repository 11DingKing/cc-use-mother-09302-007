"""命令行入口：python -m qualification --data-dir data --port 8080"""
from __future__ import annotations

import argparse

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="传承人资质巡检服务端")
    parser.add_argument("--data-dir", default="data", help="事件日志数据目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.data_dir, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
