"""agent-relay-worker CLI.

  agent-relay-worker connect CODE --server https://relay.example   # pair + stay online (most users)
  agent-relay-worker doctor                                         # what this computer can run
  agent-relay-worker run                                            # stay online (already paired)
  agent-relay-worker pair CODE --server URL                         # pair only
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
from pathlib import Path

from .adapters import all_adapters
from .worker import Api, SingleInstanceLock, State, Worker, pair

HINTS = {
    "claude_code": "export ANTHROPIC_API_KEY=...   # API key only; subscription login is never used",
    "codex": "export RELAY_CODEX_MODELS=<model,...>   # only models you may use for others' tasks",
    "http": "export RELAY_HTTP_BASE_URL=... RELAY_HTTP_API_KEY=... RELAY_HTTP_MODELS=<model,...>",
}


def doctor(state: State, adapters: dict) -> None:
    print(f"State dir: {state.root}   Paired: {'yes' if state.load() else 'no'}\n")
    usable = 0
    for name, a in adapters.items():
        det, caps = a.detect(), a.capabilities()
        if caps:
            usable += 1
            print(f"  [ready]  {name:<12} models: {', '.join(caps['models'])}")
        else:
            where = f"found {det['version']}" if det["installed"] else "not installed"
            print(f"  [off]    {name:<12} ({where})  to enable: {HINTS.get(name, '')}")
    print()
    if usable == 0:
        print("No engine is enabled yet, so this computer cannot take tasks. Set one of the variables above, then re-run.")
    else:
        print(f"{usable} engine(s) ready. Keep `agent-relay-worker run` open to receive tasks.")


def serve(state: State, adapters: dict) -> None:
    cfg = state.load()
    if not cfg:
        raise SystemExit("Not connected yet. On the website open Devices -> Connect a computer, and run the command shown.")
    with SingleInstanceLock(state.root / "worker.lock"):
        worker = Worker(Api(cfg["server"], cfg["device_token"]), state, adapters)
        if not worker.capabilities()["adapters"]:
            logging.warning("No engine enabled; this computer will not match any task. Run `agent-relay-worker doctor`.")
        print(f"Online at {cfg['server']} — leave this window open. Claim tasks from your phone; Ctrl+C to go offline.")
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            print("\nOffline.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-relay-worker")
    ap.add_argument("--state-dir", default=os.environ.get("RELAY_STATE_DIR", str(Path.home() / ".agent-relay")))
    ap.add_argument("--enable-test-adapters", action="store_true", help="offer the model-free echo adapter (dev only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd, help_ in (("connect", "pair with a code from the website, then stay online"),
                       ("pair", "pair with a code from the website")):
        p = sub.add_parser(cmd, help=help_)
        p.add_argument("code")
        p.add_argument("--server", required=True)
        p.add_argument("--name", default=platform.node() or "computer")
    sub.add_parser("doctor", help="show which engines this computer can run")
    sub.add_parser("run", help="stay online and execute claimed tasks")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    state = State(Path(args.state_dir).expanduser())
    adapters = all_adapters(enable_test=args.enable_test_adapters)

    if args.cmd in ("pair", "connect"):
        res = pair(args.server, args.code, args.name, state, adapters)
        print(f"Connected as \"{args.name}\" ({res['device_id']}).")
        if args.cmd == "pair":
            print("Next: agent-relay-worker run")
            return
        print()
        doctor(state, adapters)
        print()
        serve(state, adapters)
    elif args.cmd == "doctor":
        doctor(state, adapters)
    else:
        serve(state, adapters)


if __name__ == "__main__":
    main()
