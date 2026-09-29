#!/usr/bin/env python3
"""单条本地启动命令:  python3 app.py

- 首次启动自动播种虚构工厂数据，并生成领用 / 维修 / 报废 / 逾期样例
- SQLite: data/factory.db   点检照片: data/photos/
- 进程内守护线程每 30s 生成保养提醒、检出归还逾期
- 浏览器打开 http://127.0.0.1:8000
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from system.server import serve

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="工厂工装夹具借用与寿命记录系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 data/factory.db）")
    args = parser.parse_args()

    serve(host=args.host, port=args.port,
          db_path=Path(args.db) if args.db else None)
