"""命令行入口。

本地启动::

    python3 -m fixture_system.main           # 仅种子数据
    python3 -m fixture_system.main --samples # 同时生成领用/维修/报废样例
    ./run.sh                                 # 一条命令：建库+样例+起服务

也可直接生成样例后退出（供脚本/测试调用）::

    python3 -m fixture_system.main --make-samples
"""
from __future__ import annotations

import argparse
import os
import sys

from .db import Database
from .service import FixtureService
from . import seed
from .app import serve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="工厂工装夹具借用与寿命记录系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--samples", action="store_true",
                    help="启动时生成领用、维修、报废样例")
    ap.add_argument("--make-samples", action="store_true",
                    help="只生成样例数据后退出（不启动服务）")
    ap.add_argument("--reset", action="store_true",
                    help="删除旧库后重建")
    args = ap.parse_args(argv)

    db_path = os.path.join(args.data_dir, "fixtures.db")
    photos_dir = os.path.join(args.data_dir, "photos")
    if args.reset and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)

    if args.make_samples:
        db = Database(db_path)
        svc = FixtureService(db, photos_dir)
        seed.seed(db, svc)
        summary = seed.generate_samples(db, svc)
        svc.generate_maintenance_plans()
        svc.scan_overdue()
        print("样例数据已生成：")
        for k, v in summary.items():
            print(f"  - {k}: {v}")
        print(f"\n数据库：{os.path.abspath(db_path)}")
        return 0

    httpd = serve(args.host, args.port, args.data_dir,
                  with_samples=args.samples)
    print(f"夹具系统已启动：http://{args.host}:{args.port}")
    print("（Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
