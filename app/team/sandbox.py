"""Run generated code only where it cannot do much harm - or not at all.

What "isolated" means here, precisely (the panel shows this text):

* a fresh throw-away directory is the process's working directory and the
  only place files are staged; it is deleted afterwards;
* a scrubbed environment: no API keys, tokens, HOME or PATH leakage;
* hard CPU-time, memory, file-size, open-file and wall-clock limits;
* **no network**: Linux runs it in an empty network namespace
  (``unshare -rn``), macOS under ``sandbox-exec`` with ``(deny network*)``;
* it is NOT a filesystem jail - code can still read files the user account
  can read. That is why a source of untrusted code is never run outside
  these limits, and why this is off where no network isolation exists.

Windows has no equivalent we can apply from Python, so the sandbox reports
itself unavailable there and the Tester says so instead of pretending. A user
can explicitly opt in to running without network isolation
(``team_allow_unisolated_execution``); the panel then labels it.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

from app.team.agents import OutputError, safe_relative_path
from app.team.limits import TeamLimits

MAX_STREAM_CHARS = 12_000


@dataclass
class SandboxStatus:
    available: bool
    reason: str
    network_isolated: bool = False
    mechanism: str = ""
    pytest_available: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.available:
            return f"Unavailable: {self.reason}"
        net = "no network" if self.network_isolated else "network NOT isolated"
        return f"Available ({self.mechanism}; {net}; CPU/memory/file/time limits)"


@dataclass
class CheckResult:
    name: str
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error


_MAC_PROFILE = (
    '(version 1)(allow default)(deny network*)'
)


def _wrapper() -> tuple[list[str], bool, str]:
    """(command prefix, network isolated, mechanism)."""
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        try:
            probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=10)
            if probe.returncode == 0:
                return ["unshare", "-rn"], True, "Linux network namespace"
        except (OSError, subprocess.SubprocessError):
            pass
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        try:
            probe = subprocess.run(["sandbox-exec", "-p", _MAC_PROFILE, "true"],
                                   capture_output=True, timeout=10)
            if probe.returncode == 0:
                return ["sandbox-exec", "-p", _MAC_PROFILE], True, "macOS sandbox-exec"
        except (OSError, subprocess.SubprocessError):
            pass
    return [], False, "plain subprocess"


def probe(*, allow_unisolated: bool = False) -> SandboxStatus:
    if os.name == "nt":
        return SandboxStatus(
            False, "Windows offers no way to apply CPU/memory limits and network isolation from "
                   "here, so generated code is never run on this platform.")
    prefix, isolated, mechanism = _wrapper()
    try:
        import importlib.util
        has_pytest = importlib.util.find_spec("pytest") is not None
    except Exception:  # noqa: BLE001
        has_pytest = False
    if not isolated and not allow_unisolated:
        return SandboxStatus(
            False, "no network isolation is available on this system (needs Linux 'unshare' or "
                   "macOS 'sandbox-exec'). You can opt in to running without it in Team settings.",
            pytest_available=has_pytest)
    notes = ["Not a filesystem jail: code can read files this account can read."]
    if not isolated:
        notes.append("Running WITHOUT network isolation because you opted in.")
    return SandboxStatus(True, "", isolated, mechanism, has_pytest, notes)


# ---------------------------------------------------------------------------
# What may run
# ---------------------------------------------------------------------------

_FLAG_OK = {"-m", "-v", "-q", "-b", "-s", "-p", "-t", "-x", "--verbose", "-k", "-W", "discover"}
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-/:*]{1,120}$")


class RefusedCommand(ValueError):
    pass


def validate_command(command: list, staged: set[str], *, pytest_available: bool) -> list[str]:
    """Check a model-proposed command against the allowlist. Returns argv with
    ``python`` resolved to this interpreter. Raises RefusedCommand."""
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise RefusedCommand("command must be a list of strings")
    if command[0].lower() not in ("python", "python3", "py"):
        raise RefusedCommand("only python commands are allowed")
    rest = command[1:]
    if not rest:
        raise RefusedCommand("empty python command")
    for token in rest:
        if not _TOKEN.match(token) or token.startswith("-") and token not in _FLAG_OK:
            raise RefusedCommand(f"argument {token!r} is not allowed")
    if rest[0] == "-m":
        if len(rest) < 2 or rest[1] not in ("unittest", "py_compile", "pytest"):
            raise RefusedCommand("only -m unittest / py_compile / pytest are allowed")
        if rest[1] == "pytest" and not pytest_available:
            raise RefusedCommand("pytest is not installed here")
        if rest[1] == "py_compile":
            files = rest[2:]
            if not files:
                raise RefusedCommand("py_compile needs file names")
            for f in files:
                _require_staged(f, staged)
        return [sys.executable, "-s", *rest] if rest[1] != "pytest" else [sys.executable, *rest]
    target = rest[0]
    if target.startswith("-"):
        raise RefusedCommand("only script files and -m modules are allowed")
    _require_staged(target, staged)
    return [sys.executable, "-s", *rest]


def _require_staged(path: str, staged: set[str]) -> None:
    try:
        clean = safe_relative_path(path)
    except OutputError as exc:
        raise RefusedCommand(str(exc)) from exc
    if clean not in staged or not clean.endswith(".py"):
        raise RefusedCommand(f"{path!r} is not a staged .py file")


def default_checks(staged: set[str]) -> list[tuple[str, list[str]]]:
    """Real checks that need no model judgement: byte-compile every staged .py."""
    py = sorted(p for p in staged if p.endswith(".py"))
    if not py:
        return []
    return [("Syntax check (py_compile)", ["python", "-m", "py_compile", *py[:40]])]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _limit_resources(cpu_s: int, memory_bytes: int):
    def apply() -> None:
        import resource

        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:  # RLIMIT_AS is not enforced everywhere (notably macOS); best effort
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, OSError):
            pass
    return apply


def stage_files(root: str, files: dict[str, str]) -> set[str]:
    staged: set[str] = set()
    base = os.path.realpath(root)
    for rel, content in files.items():
        clean = safe_relative_path(rel)
        target = os.path.realpath(os.path.join(base, clean))
        if not target.startswith(base + os.sep):
            raise OutputError(f"{rel!r} escapes the sandbox directory")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        staged.add(clean)
    return staged


def run_checks(
    files: dict[str, str], checks: list[tuple[str, list]], status: SandboxStatus,
    limits: TeamLimits, *, cancelled=lambda: False,
) -> list[CheckResult]:
    """Stage ``files`` in a temp dir and run each (already validated-or-not)
    check; refused commands come back as errors, never executed."""
    if not status.available:
        raise RuntimeError("sandbox unavailable")
    prefix, _isolated, _mechanism = _wrapper() if status.network_isolated else ([], False, "")
    results: list[CheckResult] = []
    workdir = tempfile.mkdtemp(prefix="pybrowser-team-")
    try:
        staged = stage_files(workdir, files)
        for name, command in checks:
            if cancelled():
                break
            try:
                argv = validate_command(command, staged, pytest_available=status.pytest_available)
            except RefusedCommand as exc:
                results.append(CheckResult(name, list(map(str, command)), None, "", "", 0.0,
                                           error=f"Refused: {exc}"))
                continue
            results.append(_run_one(name, argv, prefix, workdir, limits, cancelled))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return results


def _clip(text: str) -> str:
    if len(text) <= MAX_STREAM_CHARS:
        return text
    return text[:MAX_STREAM_CHARS // 2] + "\n... [output truncated] ...\n" + text[-MAX_STREAM_CHARS // 2:]


def _run_one(name, argv, prefix, workdir, limits, cancelled) -> CheckResult:
    env = {
        "PATH": "/usr/bin:/bin", "HOME": workdir, "TMPDIR": workdir,
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8", "PYTHONPATH": workdir,
    }
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [*prefix, *argv], cwd=workdir, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=_limit_resources(int(limits.sandbox_cpu_s), int(limits.sandbox_memory_mb) * 1024 * 1024))
    except OSError as exc:
        return CheckResult(name, argv, None, "", "", 0.0, error=f"Could not start: {exc.strerror or exc}")
    deadline = started + limits.check_timeout_s
    timed_out = False
    while process.poll() is None:
        if cancelled() or time.monotonic() > deadline:
            timed_out = not cancelled()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            break
        time.sleep(0.05)
    try:
        out, err = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
    return CheckResult(
        name, argv, process.returncode, _clip(out.decode("utf-8", "replace")),
        _clip(err.decode("utf-8", "replace")), time.monotonic() - started, timed_out=timed_out,
        error="Timed out" if timed_out else "")


def render_report(results: list[CheckResult], status: SandboxStatus, staged_count: int) -> str:
    lines = ["# Test report", "",
             f"Sandbox: {status.summary()}", f"Files staged: {staged_count}", ""]
    if not results:
        lines.append("No checks were run.")
    for result in results:
        verdict = "PASSED" if result.passed else ("TIMED OUT" if result.timed_out else "FAILED")
        shown = " ".join(["python", *result.argv[1:]]) if result.argv else ""
        shown = shown.replace("-s ", "", 1)
        lines += [f"## {result.name}: {verdict}", f"`{shown}`",
                  f"exit code: {result.exit_code if result.exit_code is not None else 'n/a'}; "
                  f"{result.duration_s:.1f}s"]
        if result.error and not result.timed_out:
            lines.append(f"error: {result.error}")
        if result.stdout.strip():
            lines += ["", "stdout:", "```", result.stdout.strip(), "```"]
        if result.stderr.strip():
            lines += ["", "stderr:", "```", result.stderr.strip(), "```"]
        lines.append("")
    return "\n".join(lines).strip() + "\n"
