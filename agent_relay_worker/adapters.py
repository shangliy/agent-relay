"""Execution adapters (section 12): detect / probe / capabilities / execute / cancel.

Status per design-doc labels:
  echo         test-only, never for real credit
  claude_code  pending verification — `claude -p --bare`, API-key auth only
  codex        pending verification — `codex exec --json`, read-only sandbox
  http         pending verification — OpenAI-compatible /chat/completions

Usage rules: only report numbers the provider actually returned. Missing usage
is `unknown` (settles as zero), never an estimate. Cached tokens are folded in
per each provider's own contract, not by blind addition.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class ExecResult:
    status: str                      # succeeded | failed
    output_text: str = ""
    error_code: str | None = None
    usage: dict = field(default_factory=lambda: {"status": "unknown", "items": []})


def known_usage(provider: str, input_tokens: int, output_tokens: int, source: str, **extra: int) -> dict:
    items = [
        {"provider": provider, "unit": "input_tokens", "quantity": input_tokens, "source": source},
        {"provider": provider, "unit": "output_tokens", "quantity": output_tokens, "source": source},
    ]
    items += [{"provider": provider, "unit": k, "quantity": v, "source": source} for k, v in extra.items()]
    return {"status": "known", "input_tokens": input_tokens, "output_tokens": output_tokens, "items": items}


class Adapter:
    name = "base"
    usage_contract = "unknown"

    def detect(self) -> dict:
        return {"installed": False, "version": None}

    def models(self) -> list[str]:
        return []

    def capabilities(self) -> dict | None:
        """None when the adapter is not usable on this host."""
        if not self.detect()["installed"]:
            return None
        return {"models": self.models(), "usage": self.usage_contract, "version": self.detect()["version"]}

    def execute(self, prompt: str, model: str, workdir: str, timeout_s: int, cancel: threading.Event) -> ExecResult:
        raise NotImplementedError


def _run_cli(argv: list[str], prompt: str, workdir: str, timeout_s: int, cancel: threading.Event,
             env: dict | None = None) -> tuple[int | None, str, str, str | None]:
    """Run a CLI with the prompt on stdin. Returns (rc, stdout, stderr, error_code)."""
    proc = subprocess.Popen(
        argv, cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", start_new_session=True,
    )
    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def pump(stream, sink):
        for line in stream:
            sink.append(line)

    readers = [threading.Thread(target=pump, args=(proc.stdout, out_chunks), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err_chunks), daemon=True)]
    for t in readers:
        t.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    deadline = time.monotonic() + timeout_s
    error = None
    while proc.poll() is None:
        if cancel.is_set():
            error = "CANCELLED"
        elif time.monotonic() > deadline:
            error = "TIMEOUT"
        if error:
            _kill_group(proc)
            break
        time.sleep(0.2)
    proc.wait()
    for t in readers:
        t.join(timeout=5)
    return proc.returncode, "".join(out_chunks), "".join(err_chunks), error


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError, AttributeError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _isolated_env(keep: list[str]) -> dict:
    """Child env: only what the CLI needs. The relay device token is never passed down."""
    base = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "USERPROFILE") if k in os.environ}
    base.update({k: os.environ[k] for k in keep if k in os.environ})
    return base


# --------------------------------------------------------------------------- echo (tests)

class EchoAdapter(Adapter):
    """Deterministic, model-free. Counts whitespace-separated words as 'tokens' of
    its own fake model; must never be offered for real credit."""
    name = "echo"
    usage_contract = "known"

    def detect(self) -> dict:
        return {"installed": True, "version": "echo-1"}

    def models(self) -> list[str]:
        return ["echo-1"]

    def execute(self, prompt, model, workdir, timeout_s, cancel) -> ExecResult:
        if cancel.is_set():
            return ExecResult("failed", error_code="CANCELLED")
        marker = "--- ECHO_OUTPUT ---"
        out = prompt.split(marker, 1)[1].split("\n---", 1)[0].strip() if marker in prompt else prompt
        return ExecResult("succeeded", out, usage=known_usage("echo", len(prompt.split()), len(out.split()), "echo-1"))


# --------------------------------------------------------------------------- Claude Code

class ClaudeCodeAdapter(Adapter):
    """`claude -p --bare --output-format json --tools ""`.

    --bare skips hooks, plugins and user settings and authenticates only with
    ANTHROPIC_API_KEY, so subscription login quota is never used for relay work.
    """
    name = "claude_code"
    usage_contract = "known"

    def __init__(self, binary: str = "claude", models: list[str] | None = None):
        self.binary = binary
        self._models = models or [m for m in os.environ.get("RELAY_CLAUDE_MODELS", "claude-sonnet-5-5,claude-haiku-5-5").split(",") if m]

    def detect(self) -> dict:
        path = shutil.which(self.binary)
        if not path:
            return {"installed": False, "version": None}
        try:
            v = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            v = None
        return {"installed": True, "version": v}

    def capabilities(self) -> dict | None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None  # bare mode needs an API key; do not fall back to subscription auth
        return super().capabilities()

    def models(self) -> list[str]:
        return self._models

    def execute(self, prompt, model, workdir, timeout_s, cancel) -> ExecResult:
        argv = [self.binary, "-p", "--bare", "--output-format", "json", "--tools", "",
                "--no-session-persistence", "--model", model]
        rc, out, err, error = _run_cli(argv, prompt, workdir, timeout_s, cancel, _isolated_env(["ANTHROPIC_API_KEY"]))
        return self.parse(out, rc, error)

    @staticmethod
    def parse(stdout: str, rc: int | None, error: str | None) -> ExecResult:
        try:
            data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
        except ValueError:
            data = None
        usage = {"status": "unknown", "items": []}
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            u = data["usage"]
            try:
                fresh = int(u["input_tokens"])
                cache_w = int(u.get("cache_creation_input_tokens") or 0)
                cache_r = int(u.get("cache_read_input_tokens") or 0)
                # Anthropic reports cache reads/writes separately from input_tokens.
                usage = known_usage("anthropic", fresh + cache_w + cache_r, int(u["output_tokens"]), "claude_code.result",
                                    cache_creation_input_tokens=cache_w, cache_read_input_tokens=cache_r)
            except (KeyError, TypeError, ValueError):
                pass
        if error:
            return ExecResult("failed", error_code=error, usage=usage)
        if not isinstance(data, dict):
            return ExecResult("failed", error_code="UNPARSEABLE_OUTPUT", usage=usage)
        if data.get("is_error") or rc != 0:
            return ExecResult("failed", output_text=str(data.get("result") or "")[:2000],
                              error_code=f"CLI_{data.get('subtype') or 'error'}".upper()[:64], usage=usage)
        return ExecResult("succeeded", str(data.get("result") or ""), usage=usage)


# --------------------------------------------------------------------------- Codex

class CodexAdapter(Adapter):
    """`codex exec --json --sandbox read-only --ephemeral -` (prompt on stdin)."""
    name = "codex"
    usage_contract = "known"

    def __init__(self, binary: str = "codex", models: list[str] | None = None):
        self.binary = binary
        self._models = models or [m for m in os.environ.get("RELAY_CODEX_MODELS", "").split(",") if m]

    def detect(self) -> dict:
        path = shutil.which(self.binary)
        if not path:
            return {"installed": False, "version": None}
        try:
            v = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            v = None
        return {"installed": True, "version": v}

    def capabilities(self) -> dict | None:
        # Off until the operator lists models they are authorized to use for third-party tasks.
        if not self._models:
            return None
        return super().capabilities()

    def models(self) -> list[str]:
        return self._models

    def execute(self, prompt, model, workdir, timeout_s, cancel) -> ExecResult:
        argv = [self.binary, "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                "-C", workdir, "-m", model, "-"]
        env = _isolated_env(["OPENAI_API_KEY", "CODEX_HOME"])
        rc, out, err, error = _run_cli(argv, prompt, workdir, timeout_s, cancel, env)
        return self.parse(out, rc, error)

    @staticmethod
    def parse(stdout: str, rc: int | None, error: str | None) -> ExecResult:
        message, usage, failed = "", {"status": "unknown", "items": []}, None
        totals = None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                message = ev["item"].get("text") or message
            elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
                u = ev["usage"]
                t = totals or {"input": 0, "cached": 0, "output": 0}
                t["input"] += int(u.get("input_tokens") or 0)
                t["cached"] += int(u.get("cached_input_tokens") or 0)
                t["output"] += int(u.get("output_tokens") or 0)
                totals = t
            elif kind in ("turn.failed", "error"):
                failed = "CLI_TURN_FAILED"
        if totals is not None:
            # Codex input_tokens already include cached_input_tokens.
            usage = known_usage("openai", totals["input"], totals["output"], "codex.turn.completed",
                                cached_input_tokens=totals["cached"])
        if error:
            return ExecResult("failed", error_code=error, usage=usage)
        if failed or rc != 0:
            return ExecResult("failed", output_text=message, error_code=failed or "CLI_EXIT_NONZERO", usage=usage)
        return ExecResult("succeeded", message, usage=usage)


# --------------------------------------------------------------------------- OpenAI-compatible HTTP

class HttpAdapter(Adapter):
    """One /chat/completions call. Configure RELAY_HTTP_BASE_URL, RELAY_HTTP_API_KEY, RELAY_HTTP_MODELS."""
    name = "http"
    usage_contract = "known"

    def __init__(self):
        self.base = os.environ.get("RELAY_HTTP_BASE_URL", "").rstrip("/")
        self.key = os.environ.get("RELAY_HTTP_API_KEY", "")
        self._models = [m for m in os.environ.get("RELAY_HTTP_MODELS", "").split(",") if m]

    def detect(self) -> dict:
        return {"installed": bool(self.base and self._models), "version": "chat-completions"}

    def models(self) -> list[str]:
        return self._models

    def execute(self, prompt, model, workdir, timeout_s, cancel) -> ExecResult:
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(f"{self.base}/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return ExecResult("failed", error_code=f"HTTP_{e.code}")
        except (urllib.error.URLError, TimeoutError, ValueError):
            return ExecResult("failed", error_code="HTTP_UNREACHABLE")
        return self.parse(data)

    @staticmethod
    def parse(data: dict) -> ExecResult:
        usage = {"status": "unknown", "items": []}
        u = data.get("usage") or {}
        if isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
            usage = known_usage("http", u["prompt_tokens"], u["completion_tokens"], "chat.completions.usage")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ExecResult("failed", error_code="UNPARSEABLE_OUTPUT", usage=usage)
        return ExecResult("succeeded", text, usage=usage)


def all_adapters(enable_test: bool = False) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {a.name: a for a in (ClaudeCodeAdapter(), CodexAdapter(), HttpAdapter())}
    if enable_test:
        adapters["echo"] = EchoAdapter()
    return adapters
