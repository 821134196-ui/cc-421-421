#!/usr/bin/env bash
# 一条本地命令：建库（含样例）并启动夹具系统
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m fixture_system.main --reset --samples --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
