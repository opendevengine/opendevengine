"""Restricted Claude Code adapter for the synthetic M2 execution spike.

The CLI owns normal account authentication. This module never reads credentials,
transcripts, user configuration or repository instructions on the host.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from typing import Callable

from .adapter_process import (ProcessContainmentError, ProcessLimitsError, ProcessOutcome,
                              ProcessSpawnError, run_stream)

TOOLS = ("list_files", "read_file", "search_text", "write_file", "delete_file", "run_command", "ask_user")
SERVER = "opendevengine"
TRUSTED_TOOLS = frozenset(f"mcp__{SERVER}__{name}" for name in TOOLS)
# Same rule as execution profiles: a Claude Code model alias or id, never an option or path.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,79}")


class AdapterError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _PrelaunchInterrupted(Exception):
    """A prelaunch metadata query was stopped or ran into the session deadline.

    Deliberately not an OSError/ValueError, so query-specific error mapping
    cannot turn a user stop or an exhausted deadline into an auth/plugin error.
    """
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _ObservedStop:
    """Let a metadata query observe the session Event without closing it.

    run_stream sets its stop_event on every exit; the session Event must stay
    open until the agent run itself ends, so only this local view is set.
    """
    def __init__(self, outer: threading.Event):
        self.outer, self.flag = outer, False

    def is_set(self):
        return self.flag or self.outer.is_set()

    def set(self):
        self.flag = True


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value) and value >= 0:
                return value
        except OverflowError:
            pass
    return None


def _summary(value: object) -> str:
    # Text summaries are allowed; terminal controls and unbounded content are not.
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c in "\n\t" or c.isprintable())[:4000]


def _failure_code(event: dict) -> str:
    subtype = event.get("subtype")
    direct = {"error_max_turns": "turn_limit", "error_max_budget_usd": "budget_exhausted"}
    if isinstance(subtype, str) and subtype in direct:
        return direct[subtype]
    # Classify only a CLI failure, never a successful assistant's source text.
    errors = event.get("errors", [])
    text = " ".join(item[:4000] for item in errors[:8] if isinstance(item, str)) if isinstance(errors, list) else ""
    text = (text + " " + _summary(event.get("result"))).lower()
    for fragments, code in (
        (("rate_limit", "rate limit", "usage limit", "hit your limit"), "rate_limit"),
        (("authentication", "not logged in", "invalid api key"), "authentication"),
        (("context window", "context_length_exceeded", "prompt is too long"), "context_overflow"),
    ):
        if any(fragment in text for fragment in fragments):
            return code
    return "agent_error"


def _environment() -> dict[str, str]:
    # Do not inherit arbitrary tool/SDK hooks, provider credentials or debug knobs.
    names = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL")
    env = {name: os.environ[name] for name in names if name in os.environ}
    for name in ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
                 "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_POLICY_SKILLS", "CLAUDE_CODE_DISABLE_WORKFLOWS",
                 "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_DISABLE_AGENT_VIEW", "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"):
        env[name] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    return env


class ClaudeAdapter:
    capabilities = {
        "headless": True, "streaming": True, "cancellation": True,
        "resume": False, "checkpoint": False, "follow_up": False,
        "ask_user": True, "ask_user_requires_callback": True,
    }

    def __init__(self, *, claude_binary: str | None = None):
        binary = claude_binary or shutil.which("claude")
        if not binary or not Path(binary).is_absolute() or not os.access(binary, os.X_OK):
            raise AdapterError("cli_unavailable")
        self.binary = str(Path(binary).resolve())

    def _metadata(self, args, *, timeout_seconds, max_output_bytes, stop_event=None, deadline=None):
        """Each read-only CLI query gets a fresh private empty cwd.

        With a session stop_event/deadline (agent runs), the query observes the
        stop and is bounded by the shared monotonic deadline; either raises
        _PrelaunchInterrupted after run_stream has contained the query process.
        """
        bounded_by_session = False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _PrelaunchInterrupted("timeout")
            if remaining < timeout_seconds:
                timeout_seconds, bounded_by_session = remaining, True
        if stop_event is not None and stop_event.is_set():
            raise _PrelaunchInterrupted("cancelled")
        lines = []
        with tempfile.TemporaryDirectory(prefix="opendevengine-preflight-") as cwd:
            os.chmod(cwd, 0o700)
            result = run_stream(
                [self.binary, "--setting-sources", "", *args], cwd=cwd,
                env=_environment(), on_line=lines.append,
                timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes,
                stop_event=_ObservedStop(stop_event) if stop_event is not None else None,
            )
        if stop_event is not None and (result.reason == "cancelled" or stop_event.is_set()):
            raise _PrelaunchInterrupted("cancelled")
        if result.reason == "timeout" and bounded_by_session:
            raise _PrelaunchInterrupted("timeout")
        return result, b"\n".join(lines)

    def preflight(self) -> dict:
        """Read-only detection. Availability does not assert successful login/inference."""
        self._check_policy()
        try:
            profile = self._check_auth_profile()
            self._disabled_plugins()
            result, output = self._metadata(["--version"], timeout_seconds=5, max_output_bytes=1024)
        except ProcessContainmentError:
            # A metadata query whose process group could not be verified gone is
            # reported as such, not folded into an ordinary availability error.
            raise AdapterError("containment_failure") from None
        except OSError:
            raise AdapterError("cli_unavailable") from None
        if result.exit_code or result.reason != "exited" or not output:
            raise AdapterError("cli_unavailable")
        return {"available": True, "version": _summary(output.decode("utf-8", "replace").strip()),
                "authentication_verified": False, "authentication_reported": profile, "customization_metadata_checked": True, "capabilities": dict(self.capabilities)}

    @staticmethod
    def _check_policy():
        # Managed hooks cannot be disabled by --settings. This first adapter is
        # deliberately limited to unmanaged personal CLI profiles on POSIX hosts.
        if os.name != "posix":
            raise AdapterError("unsupported_host_policy")
        roots = (Path("/Library/Application Support/ClaudeCode"), Path("/etc/claude-code"),
                 Path.home() / ".claude")
        paths = [root / name for root in roots for name in
                 ("managed-settings.json", "managed-settings.d", "managed-mcp.json")]
        paths += [Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"),
                  Path("/Library/Managed Preferences") / os.environ.get("USER", "") / "com.anthropic.claudecode.plist",
                  Path.home() / "Library/Managed Preferences/com.anthropic.claudecode.plist"]
        if any(os.path.lexists(path) for path in paths):
            raise AdapterError("unsupported_managed_policy")

    def _check_auth_profile(self, *, stop_event=None, deadline=None):
        # Metadata comes from the CLI itself. Never open/copy its auth storage.
        try:
            result, output = self._metadata(["auth", "status"], timeout_seconds=10, max_output_bytes=16384,
                                            stop_event=stop_event, deadline=deadline)
            profile = json.loads(output)
        except (OSError, ValueError):
            raise AdapterError("authentication_unverified") from None
        if result.reason != "exited" or result.exit_code != 0 or not isinstance(profile, dict) or profile.get("loggedIn") is not True:
            raise AdapterError("authentication_required")
        if (profile.get("authMethod") != "claude.ai" or profile.get("apiProvider") != "firstParty" or
            profile.get("subscriptionType") not in ("pro", "max", "ultra")):
            raise AdapterError("unsupported_auth_profile")
        return {"method": "claude.ai", "personal_profile": True}

    def _disabled_plugins(self, *, stop_event=None, deadline=None):
        # Restricted mode does not suppress every installed-plugin registry entry
        # in CLI 2.1.311. Query metadata through its read-only CLI command and
        # override each entry for this invocation; never alter user settings.
        try:
            result, output = self._metadata(["plugin", "list", "--json"], timeout_seconds=10, max_output_bytes=65536,
                                            stop_event=stop_event, deadline=deadline)
            plugins = json.loads(output)
        except (OSError, ValueError):
            raise AdapterError("customization_unverified") from None
        if result.reason != "exited" or result.exit_code != 0 or not isinstance(plugins, list) or len(plugins) > 100:
            raise AdapterError("customization_unverified")
        disabled = {
            "cc-plugin-agents-md@builtin": False,
            "cc-plugin-plugin-authoring@builtin": False,
        }
        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get("scope") == "managed":
                raise AdapterError("unsupported_managed_policy")
            name = plugin.get("id")
            if not isinstance(name, str) or not name or len(name) > 256 or any(not c.isprintable() for c in name):
                raise AdapterError("customization_unverified")
            disabled[name] = False
        return disabled

    @staticmethod
    def _endpoint(endpoint: dict) -> dict:
        try:
            socket_path = Path(endpoint["socket_path"])
            token_file = Path(endpoint["token_file"])
            for path, kind in ((socket_path, stat.S_ISSOCK), (token_file, stat.S_ISREG)):
                info = path.lstat()
                parent = path.parent.lstat()
                if (not path.is_absolute() or not kind(info.st_mode) or
                    info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or
                    not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
                    stat.S_IMODE(parent.st_mode) != 0o700):
                    raise ValueError
        except (KeyError, TypeError, OSError, ValueError):
            raise AdapterError("invalid_broker_endpoint") from None
        return {"mcpServers": {SERVER: {
            "command": sys.executable,
            "args": ["-m", "opendevengine.execution.mcp_server", "--socket", str(socket_path), "--token-file", str(token_file)],
            "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        }}}

    def run(
        self, endpoint: dict, prompt: str, *, auth_mode: str,
        on_inventory_verified: Callable[[], None], emit: Callable[[str, dict], None],
        synthetic: bool = False, local_only: bool = True,
        external_source_consent: bool = False, stop_event=None, on_started=None,
        timeout_seconds: float = 180, max_turns: int = 12,
        max_budget_usd: float = 1.0, max_output_bytes: int = 524288, model: str | None = None,
    ) -> dict:
        """Run one attempt, revoking its required shared Event on every exit.

        The trusted service keeps the broker denied until on_inventory_verified.
        Determine user cancellation from durable stop_requested and returned status;
        this Event is also set after successful completion and prelaunch failures.

        timeout_seconds is one deadline for the whole attempt, including the
        prelaunch auth/plugin metadata queries, which also observe stop_event.
        Only the subprocess spawn boundary maps to cli_unavailable/invalid_limits;
        any other exception (for example LeaseLost or a persistence error from
        emit/on_inventory_verified) propagates unchanged, and only after
        run_stream confirmed the process group gone, because an unconfirmed stop
        supersedes it as a returned containment_failure.
        """
        if not isinstance(stop_event, threading.Event):
            raise AdapterError("invalid_stop_event")
        try:
            return self._run_attempt(
                endpoint, prompt, auth_mode=auth_mode,
                on_inventory_verified=on_inventory_verified, emit=emit,
                synthetic=synthetic, local_only=local_only,
                external_source_consent=external_source_consent, stop_event=stop_event,
                on_started=on_started, timeout_seconds=timeout_seconds, max_turns=max_turns,
                max_budget_usd=max_budget_usd, max_output_bytes=max_output_bytes, model=model,
            )
        except ProcessContainmentError:
            # Any process this attempt started (metadata query or agent) may still
            # be running. Return it as an unconfirmed stop, never raise it.
            return {"status": "failed", "code": "containment_failure", "exit_code": None,
                    "stop_confirmed": False, "usage": None, "reported_cost_usd": None,
                    "capabilities": dict(self.capabilities)}
        finally:
            stop_event.set()

    def _run_attempt(
        self, endpoint: dict, prompt: str, *, auth_mode: str,
        on_inventory_verified: Callable[[], None], emit: Callable[[str, dict], None],
        synthetic: bool = False, local_only: bool = True,
        external_source_consent: bool = False, stop_event=None, on_started=None,
        timeout_seconds: float = 180, max_turns: int = 12,
        max_budget_usd: float = 1.0, max_output_bytes: int = 524288, model: str | None = None,
    ) -> dict:
        """Internal single attempt; public run owns the whole-lifecycle stop guard."""
        if auth_mode != "cli_login":
            raise AdapterError("unsupported_auth_mode")
        if any(type(value) is not bool for value in (synthetic, local_only, external_source_consent)):
            raise AdapterError("invalid_source_policy")
        if not synthetic and (local_only or not external_source_consent):
            raise AdapterError("external_source_denied")
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt.encode("utf-8", "surrogatepass")) > 32768:
            raise AdapterError("invalid_prompt")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
            not 0 < timeout_seconds <= 3600 or type(max_output_bytes) is not int or
            not 1024 <= max_output_bytes <= 4_194_304):
            raise AdapterError("invalid_limits")
        try:
            prompt.encode("utf-8")
        except UnicodeError:
            raise AdapterError("invalid_prompt") from None
        if (type(max_turns) is not int or not 1 <= max_turns <= 40 or
            _number(max_budget_usd) is None or not 0 < max_budget_usd <= 10):
            raise AdapterError("invalid_limits")
        if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
            raise AdapterError("invalid_model")
        # One deadline for metadata and agent: prelaunch time is not added on top.
        deadline = time.monotonic() + timeout_seconds
        self._check_policy()
        try:
            self._check_auth_profile(stop_event=stop_event, deadline=deadline)
            disabled_plugins = self._disabled_plugins(stop_event=stop_event, deadline=deadline)
        except _PrelaunchInterrupted as interrupted:
            # Same projection as a stop/timeout observed before the agent spawned.
            return self._project(ProcessOutcome(None, interrupted.reason, True, 0), emit)
        config = self._endpoint(endpoint)
        initialized = False
        final = None
        usage = None
        cost = None
        final_summary = ""

        def consume(line: bytes):
            nonlocal initialized, final, usage, cost, final_summary
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise AdapterError("invalid_output") from None
            if not isinstance(event, dict):
                raise AdapterError("invalid_output")
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                servers = event.get("mcp_servers")
                inventory = event.get("tools")
                emit("adapter_inventory", {
                    "tools": [_summary(name)[:128] for name in inventory[:64]] if isinstance(inventory, list) else [],
                    "servers": [{"name": _summary(item.get("name"))[:128], "status": _summary(item.get("status"))[:64]}
                                for item in servers[:8] if isinstance(item, dict)] if isinstance(servers, list) else [],
                    "plugins_present": bool(event.get("plugins")),
                    "plugins": [_summary(item.get("name") or item.get("id"))[:128]
                                for item in event.get("plugins", [])[:64] if isinstance(item, dict)]
                               if isinstance(event.get("plugins"), list) else [],
                    "commands_present": bool(event.get("slash_commands")),
                    "skills_present": bool(event.get("skills")),
                    "agents": [_summary(name)[:128] for name in event.get("agents", [])[:32]]
                              if isinstance(event.get("agents", []), list) else [],
                    "output_style": _summary(event.get("output_style"))[:128],
                })
                # These fields are optional in older CLI init schemas. When
                # present, accept only the known inert default metadata. No
                # agent/skill invocation tool is permitted by the exact gate.
                builtin_agents = {"general-purpose", "statusline-setup", "Explore", "Plan", "claude-code-guide"}
                agents = event.get("agents", [])
                if (event.get("plugins") or event.get("slash_commands") or event.get("skills") or
                    not isinstance(agents, list) or any(not isinstance(name, str) or name not in builtin_agents for name in agents) or
                    ("output_style" in event and event["output_style"] != "default")):
                    raise AdapterError("unexpected_customization")
                if (initialized or not isinstance(inventory, list) or
                    any(not isinstance(name, str) for name in inventory) or
                    len(inventory) != len(TRUSTED_TOOLS) or set(inventory) != TRUSTED_TOOLS or
                    not isinstance(servers, list) or len(servers) != 1 or
                    not isinstance(servers[0], dict) or servers[0].get("name") != SERVER or
                    servers[0].get("status") != "connected"):
                    raise AdapterError("unexpected_tool_inventory")
                initialized = True
                on_inventory_verified()
                emit("adapter_ready", {"tools": sorted(TRUSTED_TOOLS), "model": _summary(event.get("model"))})
            elif kind in ("assistant", "result") and not initialized:
                raise AdapterError("missing_initialization")
            elif kind == "assistant":
                if event.get("isApiErrorMessage"):
                    return
                message = event.get("message", {})
                if not isinstance(message, dict):
                    raise AdapterError("invalid_output")
                content = message.get("content", [])
                if not isinstance(content, list):
                    raise AdapterError("invalid_output")
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        emit("assistant_summary", {"text": _summary(block.get("text"))})
                    elif block.get("type") == "tool_use":
                        name = block.get("name")
                        if not isinstance(name, str) or name not in TRUSTED_TOOLS:
                            raise AdapterError("unexpected_tool_inventory")
                        # Never persist model tool inputs, hidden reasoning or raw results.
                        emit("tool_requested", {"name": name})
            elif kind == "result":
                if final is not None:
                    raise AdapterError("invalid_output")
                subtype = event.get("subtype")
                if event.get("is_error") or subtype != "success":
                    final = {"status": "failed", "code": _failure_code(event)}
                else:
                    final = {"status": "completed", "code": "success"}
                reported = event.get("usage")
                if isinstance(reported, dict):
                    usage = {key: _number(reported.get(key)) for key in
                             ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
                cost = _number(event.get("total_cost_usd"))
                final_summary = _summary(event.get("result")) if final["status"] == "completed" else ""
            # User/tool results, thinking, hooks, progress and unknown events discarded.

        with contextlib.ExitStack() as stack:
            try:
                # Spawn preparation only; callbacks have not run yet.
                root = stack.enter_context(tempfile.TemporaryDirectory(prefix="opendevengine-adapter-"))
                os.chmod(root, 0o700)
                cwd = str(Path(root) / "cwd")
                Path(cwd).mkdir(mode=0o700)
                plugin_root = Path(root) / "plugins"
                plugin_root.mkdir(mode=0o700)
                # An unnamed private file provides finite stdin without an argv
                # disclosure, a pipe feeder thread, or a prompt pathname.
                prompt_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b", dir=root))
                os.fchmod(prompt_file.fileno(), 0o600)
                prompt_file.write(prompt.encode("utf-8"))
                prompt_file.seek(0)
            except OSError:
                raise AdapterError("cli_unavailable") from None
            argv = [self.binary, "--print", "--verbose", "--output-format", "stream-json",
                    "--restricted", "--tools", "", "--strict-mcp-config",
                    "--mcp-config", json.dumps(config), "--disable-slash-commands",
                    "--setting-sources", "", "--settings", json.dumps({
                        "disableAllHooks": True, "autoMemoryEnabled": False, "claudeMdExcludes": ["**"],
                        "syncClaudeAiSkills": False, "syncClaudeAiPlugins": False, "enabledPlugins": disabled_plugins,
                    }), "--no-session-persistence", "--no-chrome",
                    "--permission-mode", "dontAsk", "--permission-prompts", "none",
                    "--allowedTools", ",".join(sorted(TRUSTED_TOOLS)),
                    "--max-turns", str(max_turns), "--max-budget-usd", str(max_budget_usd)]
            if model is not None:
                argv += ["--model", model]
            environment = _environment()
            environment["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = str(plugin_root)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = ProcessOutcome(None, "timeout", True, 0)
            else:
                try:
                    outcome = run_stream(argv, cwd=cwd, env=environment, on_line=consume,
                                         timeout_seconds=min(remaining, timeout_seconds),
                                         max_output_bytes=max_output_bytes,
                                         stop_event=stop_event, on_started=on_started, stdin_file=prompt_file)
                # The spawn boundary only. Callback errors (emit, on_inventory_verified,
                # on_started) propagate with their own identity after containment.
                except ProcessSpawnError:
                    raise AdapterError("cli_unavailable") from None
                except ProcessLimitsError:
                    raise AdapterError("invalid_limits") from None
                except ProcessContainmentError:
                    return {"status": "failed", "code": "containment_failure", "exit_code": None,
                            "stop_confirmed": False, "usage": usage, "reported_cost_usd": cost,
                            "capabilities": dict(self.capabilities)}
        return self._project(outcome, emit, initialized=initialized, final=final, usage=usage,
                             cost=cost, final_summary=final_summary)

    def _project(self, outcome, emit, *, initialized=False, final=None, usage=None, cost=None, final_summary=""):
        """Map a contained process outcome to the public result and its single event."""
        if not outcome.stop_confirmed:
            result = {"status": "failed", "code": "containment_failure"}
        elif outcome.reason != "exited":
            result = {"status": "cancelled" if outcome.reason == "cancelled" else "failed", "code": outcome.reason}
        elif outcome.exit_code != 0:
            result = {"status": "failed", "code": "cli_exit"}
        elif not initialized or final is None:
            result = {"status": "failed", "code": "invalid_output"}
        else:
            result = final
        emit("adapter_result", {**result, "summary": final_summary, "usage": usage, "reported_cost_usd": cost})
        return {**result, "exit_code": outcome.exit_code, "stop_confirmed": outcome.stop_confirmed,
                "usage": usage, "reported_cost_usd": cost, "capabilities": dict(self.capabilities)}
