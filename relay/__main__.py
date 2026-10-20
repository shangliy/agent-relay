"""Run the control plane: python -m relay [--host 127.0.0.1] [--port 8787] [--db agent_relay.db]"""

from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-relay-server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=None, help="SQLite path (default: $RELAY_DB or agent_relay.db)")
    args = ap.parse_args()
    settings = Settings()
    if args.db:
        settings.db_path = args.db
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
