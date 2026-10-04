"""Run generated code only inside a verified, genuinely isolated environment -
or not at all.

There is deliberately NO fallback to running on the host unconfined, and no
setting that enables one. A backend counts as available only if, at probe
time, it passes a self-test proving the three things that matter:

* **host files are hidden** - a canary file the probe planted in your home
  folder cannot be read from inside;
* **the host network is unreachable** - a listener the probe opened on
  127.0.0.1 cannot be connected to from inside;
* **only the scratch directory is writable** - and it is writable.

Backends (chosen automatically; ``team_sandbox_backend`` can force one):

``bubblewrap`` (Linux)
    Namespaces for user/pid/ipc/net/uts, a read-only view of only the system
    directories and the Python installation, an empty ``/tmp``, and the
    throw-away work directory. Your home folder and the application itself do
    not exist inside.
``seatbelt`` (macOS)
    ``sandbox-exec`` with a profile that denies the network, denies reading
    user data folders and denies all writes except the work directory.
``container`` (Windows, and any OS)
    Docker or Podman: ``--network none``, read-only root filesystem, all
    capabilities dropped, no-new-privileges, pid/memory/CPU/file limits, a
    non-root user, and only the work directory mounted. The image must already
    be present (``--pull never``) - the sandbox never reaches the network to
    fetch one behind your back.

On top of that, every run has CPU/memory/file-size/wall-clock limits, a
scrubbed environment (no keys), and commands are allow-listed to Python
unittest / py_compile / pytest and staged ``.py`` scripts.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

from app.team.agents import OutputError, safe_relative_path
from app.team.limits import TeamLimits

MAX_STREAM_CHARS = 12_000
DEFAULT_IMAGE = "python:3.12-slim"
BACKENDS = ("auto", "native", "container")


@dataclass
class SandboxStatus:
    available: bool
    reason: str
    backend: str = "none"
    mechanism: str = ""
    network_isolated: bool = False
    fs_confined: bool = False
    pytest_available: bool = False
    #: What to do to make it available, in words the user can follow.
    setup_hint: str = ""
    checking: bool = False
    notes: list[str] = field(default_factory=list)
    _backend: object = field(default=None, repr=False, compare=False)

    def summary(self) -> str:
        if self.checking:
            return "Checking…"
        if not self.available:
            return f"Unavailable: {self.reason}"
        return (f"Available ({self.mechanism}; no network; host files hidden; "
                "CPU/memory/file/time limits)")


#: Windows: without this every docker call from the windowed app flashes a console.
_NO_WINDOW: dict = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {})


def _bind_mount(source: str, target: str) -> str:
    """``--mount`` value. The option is comma-separated, so a Windows profile
    folder such as ``C:\\Users\\Doe, Jane\\AppData\\Local\\Temp`` must be CSV-quoted."""
    if "," in source or '"' in source:
        return 'type=bind,"source=%s",target=%s' % (source.replace('"', '""'), target)
    return f"type=bind,source={source},target={target}"


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


def checking_status() -> SandboxStatus:
    return SandboxStatus(False, "still checking", checking=True)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _limit_resources(cpu_s: int, memory_bytes: int):
    def apply() -> None:
        import resource

        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:  # not enforced everywhere (notably macOS); best effort
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, OSError):
            pass
    return apply


class _Backend:
    name = ""
    mechanism = ""
    #: Whether the work directory is the host process's cwd (native) or only
    #: exists inside the container.
    uses_host_cwd = True

    def command(self, tokens: list[str], workdir: str, limits: TeamLimits, run_id: str) -> list[str]:
        raise NotImplementedError

    def env(self, workdir: str) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "HOME": workdir, "TMPDIR": workdir,
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}

    def popen_kwargs(self, limits: TeamLimits) -> dict:
        if os.name == "nt":
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        return {"preexec_fn": _limit_resources(int(limits.sandbox_cpu_s),
                                               int(limits.sandbox_memory_mb) * 1024 * 1024)}

    def kill(self, process: subprocess.Popen, run_id: str) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass

    def stage_permissions(self, root: str) -> None:
        pass


def _python_prefixes() -> list[str]:
    seen: list[str] = []
    for path in (sys.prefix, sys.base_prefix, os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))):
        real = os.path.realpath(path)
        if os.path.isdir(real) and real not in seen and real not in ("/", ""):
            seen.append(real)
    return seen


class _Bubblewrap(_Backend):
    name = "bubblewrap"
    mechanism = "Linux bubblewrap namespaces"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def command(self, tokens, workdir, limits, run_id):
        args = [self.executable, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
                "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        # Only system directories and the Python installation are visible,
        # read-only. Home, the application and the rest of the disk do not exist.
        for path in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64"):
            args += ["--ro-bind-try", path, path]
        for prefix in _python_prefixes():
            args += ["--ro-bind-try", prefix, prefix]
        args += ["--bind", workdir, "/work", "--chdir", "/work"]
        for key, value in {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                           "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}.items():
            args += ["--setenv", key, value]
        return [*args, sys.executable, *tokens[1:]]

    def env(self, workdir):
        return {"PATH": "/usr/bin:/bin"}


class _Seatbelt(_Backend):
    name = "seatbelt"
    mechanism = "macOS sandbox-exec"

    def profile(self, workdir: str) -> str:
        def sub(path: str) -> str:
            return '(subpath "%s")' % path.replace("\\", "\\\\").replace('"', '\\"')

        reads = " ".join(sub(p) for p in [*_python_prefixes(), os.path.realpath(workdir)])
        return (
            "(version 1)(allow default)(deny network*)"
            "(deny file-read* (subpath \"/Users\") (subpath \"/Volumes\") (subpath \"/private/var/root\")"
            " (subpath \"/Library/Keychains\") (subpath \"/private/etc/ssh\"))"
            f"(allow file-read* {reads})"
            "(deny file-write*)"
            f"(allow file-write* {sub(os.path.realpath(workdir))} (literal \"/dev/null\"))")

    def command(self, tokens, workdir, limits, run_id):
        return ["sandbox-exec", "-p", self.profile(workdir), sys.executable, *tokens[1:]]


class _Container(_Backend):
    name = "container"
    uses_host_cwd = False

    def __init__(self, runtime: str, image: str) -> None:
        self.runtime = runtime
        self.image = image
        self.mechanism = f"{os.path.basename(runtime).replace('.exe', '')} container ({image})"

    def command(self, tokens, workdir, limits, run_id):
        if "," in workdir or '"' in workdir:
            raise RuntimeError("The temporary directory path contains a character the container "
                               "runtime cannot mount safely.")
        mem = f"{int(limits.sandbox_memory_mb)}m"
        cpu = int(limits.sandbox_cpu_s)
        return [
            self.runtime, "run", "--rm", "--name", run_id, "--pull", "never",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", mem, "--memory-swap", mem, "--cpus", "1",
            "--ulimit", "nofile=256:256", "--ulimit", "fsize=10485760:10485760",
            "--ulimit", f"cpu={cpu}:{cpu}", "--user", "65534:65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", _bind_mount(workdir, "/work"),
            "--workdir", "/work",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONNOUSERSITE=1",
            "--env", "PYTHONIOENCODING=utf-8",
            self.image, "python", *tokens[1:],
        ]

    def env(self, workdir):
        # The runtime CLI needs the user's normal environment to find its
        # daemon; nothing from it is passed INTO the container (no --env-host).
        return dict(os.environ)

    def popen_kwargs(self, limits):
        if os.name == "nt":
            return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        return {"start_new_session": True}

    def kill(self, process, run_id):
        try:
            subprocess.run([self.runtime, "rm", "-f", run_id], capture_output=True, timeout=15, **_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.kill()
        except OSError:
            pass

    def stage_permissions(self, root: str) -> None:
        # The container runs as an unprivileged user; this directory is a
        # throw-away copy, so making it writable to that user costs nothing.
        for current, dirs, _files in os.walk(root):
            os.chmod(current, 0o777)


# ---------------------------------------------------------------------------
# Probing: availability is something a backend PROVES, not something we assume
# ---------------------------------------------------------------------------

_SELFTEST = r'''
import os, socket, sys
canary, port = sys.argv[1], int(sys.argv[2])
out = []
try:
    open(canary).read(); out.append("host-file-readable")
except Exception:
    out.append("host-file-hidden")
try:
    socket.create_connection(("127.0.0.1", port), timeout=2).close(); out.append("host-network-open")
except Exception:
    out.append("host-network-blocked")
try:
    open("probe.txt", "w").write("x"); out.append("workdir-writable")
except Exception:
    out.append("workdir-readonly")
try:
    open("/etc/hostname").read(); out.append("etc-readable")
except Exception:
    pass
try:
    import pytest; out.append("pytest")
except Exception:
    pass
print(" ".join(out))
'''

_probe_lock = threading.Lock()


def _self_test(backend: _Backend, limits: TeamLimits) -> tuple[bool, str, set[str]]:
    """Run the self-test inside ``backend``. Returns (ok, why-not, flags)."""
    home = os.path.expanduser("~")
    canary_dir = home if os.path.isdir(home) else tempfile.gettempdir()
    canary = os.path.join(canary_dir, f".pybrowser-sandbox-canary-{uuid.uuid4().hex}")
    listener = socket.socket()
    workdir = tempfile.mkdtemp(prefix="pybrowser-team-probe-")
    try:
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("host secret")
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        backend.stage_permissions(workdir)
        with open(os.path.join(workdir, "selftest.py"), "w", encoding="utf-8") as handle:
            handle.write(_SELFTEST)
        backend.stage_permissions(workdir)
        probe_limits = TeamLimits(check_timeout_s=40.0, sandbox_memory_mb=max(256, limits.sandbox_memory_mb),
                                  sandbox_cpu_s=30).clamped()
        result = _run_one("selftest", ["python", "-s", "selftest.py", canary, str(port)],
                          backend, workdir, probe_limits, lambda: False)
    except OSError as exc:
        return False, f"self-test could not run: {exc}", set()
    except RuntimeError as exc:
        return False, str(exc), set()
    finally:
        listener.close()
        try:
            os.unlink(canary)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)
    if result.timed_out or result.exit_code != 0:
        tail = (result.stderr or result.error or "").strip().splitlines()[-1:] or ["no output"]
        return False, f"the self-test did not run inside it ({tail[0][:160]})", set()
    flags = set((result.stdout or "").split())
    if "host-file-readable" in flags:
        return False, "it can read files in your home folder, so it is NOT isolated", flags
    if "host-network-open" in flags:
        return False, "it can reach the host network, so it is NOT isolated", flags
    if "workdir-writable" not in flags:
        return False, "its scratch directory is not writable", flags
    return True, "", flags


def _native_backend() -> tuple[_Backend | None, str, str]:
    """(backend, why-not, hint)"""
    if sys.platform.startswith("linux"):
        exe = shutil.which("bwrap")
        if not exe:
            return None, "bubblewrap ('bwrap') is not installed.", \
                "Install it: sudo apt install bubblewrap  (Fedora: sudo dnf install bubblewrap)."
        return _Bubblewrap(exe), "", ""
    if sys.platform == "darwin":
        if not shutil.which("sandbox-exec"):
            return None, "sandbox-exec is not available.", "Use the container backend (Docker Desktop)."
        return _Seatbelt(), "", ""
    return None, "There is no built-in sandbox on this platform.", ""


def container_runtime() -> str | None:
    for name in ("docker", "podman"):
        found = shutil.which(name)
        if found:
            return found
    return None


def setup_instructions(image: str = DEFAULT_IMAGE, *, windows: bool | None = None) -> str:
    if (sys.platform == "win32") if windows is None else windows:
        return ("Windows needs a container to run generated code safely:\n"
                "1. Install Docker Desktop (https://www.docker.com/products/docker-desktop/) with the "
                "WSL 2 backend and start it. Keep it in Linux containers mode (the default).\n"
                f"2. In a terminal run:  docker pull {image}\n"
                "3. Come back and press 'Check sandbox' in Limits and model (or reopen the Team tab).")
    return (f"To use a container: install Docker or Podman, make sure its daemon is running, then run:  "
            f"docker pull {image}")


def _container_backend(image: str) -> tuple[_Backend | None, str, str]:
    runtime = container_runtime()
    hint = setup_instructions(image)
    if runtime is None:
        return None, "Docker/Podman was not found on PATH.", hint
    try:
        info = subprocess.run([runtime, "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=15, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run {os.path.basename(runtime)}: {exc}", hint
    if info.returncode != 0 or not info.stdout.strip():
        return None, "the container daemon is not running (start Docker Desktop / the Docker service).", hint
    try:
        have = subprocess.run([runtime, "image", "inspect", image], capture_output=True, timeout=15, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not inspect the image: {exc}", hint
    if have.returncode != 0:
        return None, f"the sandbox image '{image}' is not downloaded.", \
            f"Download it once:  {os.path.basename(runtime)} pull {image}  (the sandbox never pulls images by itself)."
    return _Container(runtime, image), "", ""


_cache: dict[tuple, SandboxStatus] = {}


def probe(preferred: str = "auto", image: str = DEFAULT_IMAGE, *, limits: TeamLimits | None = None,
          use_cache: bool = True) -> SandboxStatus:
    """Find a backend and PROVE it isolates. Never falls back to the host."""
    preferred = preferred if preferred in BACKENDS else "auto"
    key = (preferred, image, sys.platform)
    with _probe_lock:
        if use_cache and key in _cache:
            return _cache[key]
        limits = limits or TeamLimits()
        failures: list[str] = []
        hints: list[str] = []
        candidates: list[tuple[str, tuple]] = []
        if preferred in ("auto", "native"):
            candidates.append(("native", _native_backend()))
        if preferred in ("auto", "container"):
            candidates.append(("container", _container_backend(image)))
        status: SandboxStatus | None = None
        for label, (backend, why, hint) in candidates:
            if backend is None:
                failures.append(f"{label}: {why}")
                if hint:
                    hints.append(hint)
                continue
            ok, problem, flags = _self_test(backend, limits)
            if ok:
                notes = []
                if "etc-readable" in flags:
                    notes.append("Read-only system files (/etc) are visible inside; your own files are not.")
                status = SandboxStatus(True, "", backend.name, backend.mechanism, True, True,
                                       "pytest" in flags, "", False, notes, backend)
                break
            failures.append(f"{backend.name}: {problem}")
            hints.append(setup_instructions(image) if backend.name == "container" else "")
        if status is None:
            status = SandboxStatus(
                False, "; ".join(failures) or "no isolation backend is available.",
                setup_hint="\n".join(h for h in dict.fromkeys(hints) if h) or setup_instructions(image))
        _cache[key] = status
        return status


def pull_image(image: str = DEFAULT_IMAGE, timeout: float = 900.0) -> tuple[bool, str]:
    """``docker pull`` - only ever called from an explicit user action."""
    runtime = container_runtime()
    if runtime is None:
        return False, "Docker/Podman was not found on PATH."
    try:
        result = subprocess.run([runtime, "pull", image], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    _cache.clear()
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[-300:]
    return True, f"Downloaded {image}."


# ---------------------------------------------------------------------------
# What may run
# ---------------------------------------------------------------------------

_FLAG_OK = {"-m", "-v", "-q", "-b", "-s", "-p", "-t", "-x", "--verbose", "-k", "-W", "discover"}
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-/:*]{1,120}$")


class RefusedCommand(ValueError):
    pass


def validate_command(command: list, staged: set[str], *, pytest_available: bool) -> list[str]:
    """Check a model-proposed command against the allowlist. Returns tokens
    starting with the literal ``python`` (each backend picks the interpreter).
    Raises RefusedCommand."""
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
            raise RefusedCommand("pytest is not installed in the sandbox")
        if rest[1] == "py_compile":
            files = rest[2:]
            if not files:
                raise RefusedCommand("py_compile needs file names")
            for f in files:
                _require_staged(f, staged)
        if rest[1] == "pytest":
            return ["python", *rest, "-p", "no:cacheprovider"]
        return ["python", "-s", *rest]
    target = rest[0]
    if target.startswith("-"):
        raise RefusedCommand("only script files and -m modules are allowed")
    _require_staged(target, staged)
    return ["python", "-s", *rest]


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
    """Stage ``files`` in a temp dir and run each check inside the verified
    backend; refused commands come back as errors, never executed. Raises
    RuntimeError if the sandbox is not available - there is no host fallback."""
    backend = status._backend
    if not status.available or backend is None:
        raise RuntimeError("sandbox unavailable")
    results: list[CheckResult] = []
    workdir = tempfile.mkdtemp(prefix="pybrowser-team-")
    try:
        staged = stage_files(workdir, files)
        backend.stage_permissions(workdir)
        for name, command in checks:
            if cancelled():
                break
            try:
                tokens = validate_command(command, staged, pytest_available=status.pytest_available)
            except RefusedCommand as exc:
                results.append(CheckResult(name, list(map(str, command)), None, "", "", 0.0,
                                           error=f"Refused: {exc}"))
                continue
            results.append(_run_one(name, tokens, backend, workdir, limits, cancelled))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return results


def _clip(text: str) -> str:
    if len(text) <= MAX_STREAM_CHARS:
        return text
    return text[:MAX_STREAM_CHARS // 2] + "\n... [output truncated] ...\n" + text[-MAX_STREAM_CHARS // 2:]


def _run_one(name, tokens, backend: _Backend, workdir, limits, cancelled) -> CheckResult:
    run_id = f"pybrowser-team-{uuid.uuid4().hex[:12]}"
    started = time.monotonic()
    try:
        argv = backend.command(tokens, workdir, limits, run_id)
        process = subprocess.Popen(
            argv, cwd=workdir if backend.uses_host_cwd else None, env=backend.env(workdir),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **backend.popen_kwargs(limits))
    except RuntimeError as exc:
        return CheckResult(name, list(tokens), None, "", "", 0.0, error=str(exc))
    except OSError as exc:
        return CheckResult(name, list(tokens), None, "", "", 0.0,
                           error=f"Could not start: {exc.strerror or exc}")
    deadline = started + limits.check_timeout_s
    timed_out = False
    # Drain the pipes on threads so a chatty process cannot block on a full pipe.
    buffers: dict[str, bytes] = {"out": b"", "err": b""}

    def drain(stream, key):
        try:
            buffers[key] = stream.read()
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True)]
    for reader in readers:
        reader.start()
    while process.poll() is None:
        if cancelled() or time.monotonic() > deadline:
            timed_out = not cancelled()
            backend.kill(process, run_id)
            break
        time.sleep(0.05)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    for reader in readers:
        reader.join(timeout=5)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return CheckResult(
        name, list(tokens), process.returncode, _clip(buffers["out"].decode("utf-8", "replace")),
        _clip(buffers["err"].decode("utf-8", "replace")), time.monotonic() - started,
        timed_out=timed_out, error="Timed out" if timed_out else "")


def render_report(results: list[CheckResult], status: SandboxStatus, staged_count: int) -> str:
    lines = ["# Test report", "",
             f"Sandbox: {status.summary()}", f"Files staged: {staged_count}", ""]
    if not results:
        lines.append("No checks were run.")
    for result in results:
        verdict = "PASSED" if result.passed else ("TIMED OUT" if result.timed_out else "FAILED")
        shown = " ".join(result.argv).replace(" -s ", " ", 1)
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
