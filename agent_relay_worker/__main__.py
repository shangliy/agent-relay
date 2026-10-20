"""agent-relay-worker CLI.

  python -m agent_relay_worker pair CODE --server http://127.0.0.1:8787
  python -m agent_relay_worker doctor
  python -m agent_relay_worker run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .adapters import all_adapters
from .worker import Api, SingleInstanceLock, State, Worker, pair


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-relay-worker")
    ap.add_argument("--state-dir", default=os.environ.get("RELAY_STATE_DIR", str(Path.home() / ".agent-relay")))
    ap.add_argument("--enable-test-adapters", action="store_true", help="offer the model-free echo adapter (dev only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="exchange a web pair code for a device key")
    p.add_argument("code")
    p.add_argument("--server", required=True)
    p.add_argument("--name", default=os.uname().nodename if hasattr(os, "uname") else "device")
    sub.add_parser("doctor", help="show detected adapters and what this device will advertise")
    sub.add_parser("run", help="stay online and execute claimed tasks")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    state = State(Path(args.state_dir).expanduser())
    adapters = all_adapters(enable_test=args.enable_test_adapters)

    if args.cmd == "pair":
        res = pair(args.server, args.code, args.name, state, adapters)
        print(f"Paired as device {res['device_id']}. Run: python -m agent_relay_worker run")
        return

    if args.cmd == "doctor":
        report = {name: {"detect": a.detect(), "advertised": a.capabilities()} for name, a in adapters.items()}
        print(json.dumps({"state_dir": str(state.root), "paired": bool(state.load()), "adapters": report},
                         indent=2, ensure_ascii=False))
        return

    cfg = state.load()
    if not cfg:
        raise SystemExit("Not paired. Generate a code on the web, then: agent-relay-worker pair CODE --server URL")
    with SingleInstanceLock(state.root / "worker.lock"):
        worker = Worker(Api(cfg["server"], cfg["device_token"]), state, adapters)
        if not worker.capabilities()["adapters"]:
            logging.warning("No usable adapters detected; run `doctor`. The device will not match any task.")
        worker.run_forever()


if __name__ == "__main__":
    main()
