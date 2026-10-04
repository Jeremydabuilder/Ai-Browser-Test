"""The Tester's sandbox: real isolation where this machine has it, a refusal
to run anywhere that cannot prove it, and the container backend (against a
stub ``docker``, since no daemon is available in CI sandboxes).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_team_sandbox -v
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.team import sandbox as sb  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE = sb.probe("native", use_cache=False)


def run_script(status, source: str, **limit_kwargs) -> sb.CheckResult:
    limits = TeamLimits(**limit_kwargs) if limit_kwargs else TeamLimits()
    return sb.run_checks({"t.py": source}, [("t", ["python", "t.py"])], status, limits)[0]


@unittest.skipUnless(NATIVE.available, "no built-in sandbox here: " + NATIVE.reason)
class NativeSandboxIsolationTests(unittest.TestCase):
    """The strongest claims, tested against the real thing."""

    def test_the_probe_proved_isolation_before_declaring_it_available(self) -> None:
        self.assertTrue(NATIVE.available and NATIVE.fs_confined and NATIVE.network_isolated)
        self.assertIn(NATIVE.backend, ("bubblewrap", "seatbelt"))

    def test_only_system_dirs_the_interpreter_and_the_work_dir_exist_inside(self) -> None:
        home_marker = os.path.join(os.path.expanduser("~"), f".pybrowser-test-marker-{os.getpid()}")
        with open(home_marker, "w", encoding="utf-8") as handle:
            handle.write("host secret")
        try:
            result = run_script(NATIVE, textwrap.dedent(f'''
                import os
                def t(label, f):
                    try: print(label, "->", f())
                    except Exception as e: print(label, "-> blocked", type(e).__name__)
                t("home-marker", lambda: open({home_marker!r}).read())
                t("project", lambda: os.listdir({ROOT!r}))
                t("passwd", lambda: open("/etc/passwd").read())
                t("ssh", lambda: os.listdir(os.path.expanduser("~/.ssh")))
                t("write-usr", lambda: open("/usr/pwned", "w"))
                t("write-work", lambda: open("ok.txt", "w").write("1"))
                t("env", lambda: [k for k in os.environ if "KEY" in k or "TOKEN" in k or "SECRET" in k])
            '''))
        finally:
            os.unlink(home_marker)
        out = result.stdout
        self.assertIn("home-marker -> blocked", out)
        self.assertIn("project -> blocked", out)
        self.assertIn("passwd -> blocked", out)
        self.assertIn("ssh -> blocked", out)
        self.assertIn("write-usr -> blocked", out)
        self.assertIn("write-work -> 1", out)
        self.assertIn("env -> []", out)
        self.assertNotIn("host secret", out)

    def test_the_host_network_and_loopback_services_are_unreachable(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        try:
            result = run_script(NATIVE, textwrap.dedent(f'''
                import socket
                for addr in (("127.0.0.1", {port}), ("1.1.1.1", 53), ("::1", {port})):
                    try:
                        socket.create_connection(addr, timeout=2).close(); print("OPEN", addr)
                    except OSError as e:
                        print("closed", addr[0])
            '''))
        finally:
            listener.close()
        self.assertNotIn("OPEN", result.stdout)
        self.assertEqual(result.stdout.count("closed"), 3)

    def test_runaway_code_is_stopped_by_the_limits(self) -> None:
        started = time.monotonic()
        spin = run_script(NATIVE, "while True:\n    pass\n", check_timeout_s=5.0)
        self.assertTrue(spin.timed_out)
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse(run_script(NATIVE, "x = bytearray(2 * 1024**3)\nprint('allocated')\n",
                                    sandbox_memory_mb=256).passed)
        big = run_script(NATIVE, "open('big.bin','wb').write(b'0' * (50 * 1024 * 1024))\nprint('wrote')\n")
        self.assertFalse(big.passed)

    def test_a_real_unittest_run_works_and_failures_are_reported_verbatim(self) -> None:
        files = {"calc.py": "def add(a, b):\n    return a - b\n",
                 "test_calc.py": ("import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
                                  "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")}
        result = sb.run_checks(files, [("unit", ["python", "-m", "unittest", "-v", "test_calc"])],
                               NATIVE, TeamLimits())[0]
        self.assertFalse(result.passed)
        self.assertIn("AssertionError: -1 != 5", result.stderr)


class ProbeNeverTrustsABackendTests(unittest.TestCase):
    """If a backend does not actually isolate, the probe must say so and the
    runner must refuse - there is no host fallback."""

    class Leaky(sb._Backend):
        name = "leaky"
        mechanism = "no isolation at all"

        def command(self, tokens, workdir, limits, run_id):
            return [sys.executable, *tokens[1:]]

    def probe_with(self, backend) -> sb.SandboxStatus:
        with mock.patch.object(sb, "_native_backend", return_value=(backend, "", "")):
            return sb.probe("native", use_cache=False)

    def test_a_backend_that_can_read_your_home_folder_is_rejected(self) -> None:
        status = self.probe_with(self.Leaky())
        self.assertFalse(status.available)
        self.assertIn("can read files in your home folder", status.reason)
        with self.assertRaises(RuntimeError):
            sb.run_checks({"a.py": "print(1)"}, [("c", ["python", "a.py"])], status, TeamLimits())

    @unittest.skipUnless(NATIVE.available and NATIVE.backend == "bubblewrap", "needs bubblewrap")
    def test_a_backend_that_hides_files_but_shares_the_network_is_rejected(self) -> None:
        class SharesNetwork(sb._Bubblewrap):
            def command(self, tokens, workdir, limits, run_id):
                argv = super().command(tokens, workdir, limits, run_id)
                argv.insert(argv.index("--unshare-all") + 1, "--share-net")
                return argv

        status = self.probe_with(SharesNetwork(NATIVE._backend.executable))
        self.assertFalse(status.available)
        self.assertIn("host network", status.reason)

    def test_with_no_backend_the_status_explains_how_to_get_one(self) -> None:
        with mock.patch.object(sb, "_native_backend", return_value=(None, "bubblewrap is missing", "install it")), \
                mock.patch.object(sb, "container_runtime", return_value=None):
            status = sb.probe("auto", use_cache=False)
        self.assertFalse(status.available)
        self.assertIn("bubblewrap is missing", status.reason)
        self.assertIn("Docker/Podman was not found", status.reason)
        self.assertIn("install it", status.setup_hint)

    def test_windows_instructions_name_docker_desktop_and_the_pull_command(self) -> None:
        text = sb.setup_instructions("python:3.12-slim", windows=True)
        self.assertIn("Docker Desktop", text)
        self.assertIn("docker pull python:3.12-slim", text)
        self.assertIn("WSL 2", text)

    def test_on_windows_the_only_route_is_a_container_and_the_status_says_how_to_get_one(self) -> None:
        import types
        fake_sys = types.SimpleNamespace(platform="win32", executable=sys.executable, prefix=sys.prefix,
                                         base_prefix=sys.base_prefix)
        with mock.patch.object(sb, "sys", fake_sys), mock.patch.object(sb, "container_runtime", return_value=None):
            native, why, _hint = sb._native_backend()
            status = sb.probe("auto", use_cache=False)
        self.assertIsNone(native)
        self.assertIn("no built-in sandbox", why)
        self.assertFalse(status.available)                          # never falls back to the host
        self.assertIn("Docker/Podman was not found", status.reason)
        self.assertIn("Docker Desktop", status.setup_hint)
        self.assertIn("docker pull python:3.12-slim", status.setup_hint)
        self.assertIn("Linux containers", status.setup_hint)
        with self.assertRaises(RuntimeError):
            sb.run_checks({"a.py": "print(1)"}, [("c", ["python", "a.py"])], status, TeamLimits())

    def test_the_tester_is_dropped_and_stated_when_the_sandbox_is_unavailable(self) -> None:
        from tests.test_team_engine import (
            APPROVE, CODE_PLAN, FakeClient, make, Capabilities,
        )
        status = sb.SandboxStatus(False, "no isolation backend is available.")
        client = FakeClient({"plan": [CODE_PLAN], "coder": [json.dumps(
            {"summary": "s", "files": [{"path": "a.py", "content": "x = 1\n"}]})],
            "reviewer": [APPROVE], "final": ["ok"]})
        engine, mission = make(client, goal="Write a python module", srcs=[], caps=Capabilities(status))
        engine.run()
        self.assertNotIn("tester", [t.agent for t in mission.tasks])
        self.assertTrue(any("Tests were not run" in x for x in mission.limitations))


STUB = '''#!{python}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
mode, image = os.environ.get("STUB_MODE", "isolated"), os.environ["STUB_IMAGE"]
if argv[:1] == ["version"]:
    if mode == "down":
        sys.stderr.write("Cannot connect to the Docker daemon"); sys.exit(1)
    print("29.0.0"); sys.exit(0)
if argv[:2] == ["image", "inspect"]:
    sys.exit(1 if mode == "noimage" else 0)
if argv[:2] == ["rm", "-f"]:
    sys.exit(0)
if argv[:1] == ["run"]:
    i = argv.index(image)
    options, command = argv[1:i], argv[i + 1:]
    source = None
    for j, a in enumerate(options):
        if a == "--mount":
            for part in options[j + 1].split(","):
                if part.startswith("source="):
                    source = part[len("source="):]
    if mode == "leaky":
        os.chdir(source)
        os.execv(sys.executable, [sys.executable] + command[1:])
    sys.path.insert(0, os.environ["STUB_ROOT"])
    from app.team.sandbox import _Bubblewrap
    from app.team.limits import TeamLimits
    full = _Bubblewrap("bwrap").command(["python"] + command[1:], source, TeamLimits(), "stub")
    os.execvp("bwrap", full)
sys.exit(2)
'''


class ContainerBackendTests(unittest.TestCase):
    """The Docker/Podman path - what Windows uses - against a stub runtime."""

    IMAGE = "python:3.12-slim"

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.stub = os.path.join(self.dir.name, "docker")
        with open(self.stub, "w", encoding="utf-8") as handle:
            handle.write(STUB.format(python=sys.executable))
        os.chmod(self.stub, os.stat(self.stub).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.dir.name, "log.jsonl")
        patcher = mock.patch.dict(os.environ, {"STUB_LOG": self.log, "STUB_IMAGE": self.IMAGE, "STUB_ROOT": ROOT})
        patcher.start()
        self.addCleanup(patcher.stop)
        which = mock.patch.object(sb, "container_runtime", return_value=self.stub)
        which.start()
        self.addCleanup(which.stop)
        self.addCleanup(self.dir.cleanup)

    def calls(self) -> list[list[str]]:
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def probe(self, mode: str) -> sb.SandboxStatus:
        with mock.patch.dict(os.environ, {"STUB_MODE": mode}):
            return sb.probe("container", self.IMAGE, use_cache=False)

    def test_a_stopped_daemon_and_a_missing_image_each_say_exactly_what_to_do(self) -> None:
        down = self.probe("down")
        self.assertFalse(down.available)
        self.assertIn("daemon is not running", down.reason)
        missing = self.probe("noimage")
        self.assertFalse(missing.available)
        self.assertIn("is not downloaded", missing.reason)
        self.assertIn(f"pull {self.IMAGE}", missing.setup_hint)
        self.assertIn("never pulls images by itself", missing.setup_hint)
        self.assertFalse(any(c[:1] == ["run"] for c in self.calls()))   # nothing ran without a daemon/image

    def test_a_container_runtime_that_does_not_isolate_is_rejected_by_the_self_test(self) -> None:
        status = self.probe("leaky")
        self.assertFalse(status.available)
        self.assertIn("NOT isolated", status.reason)

    @unittest.skipUnless(NATIVE.available and NATIVE.backend == "bubblewrap",
                         "the stub emulates a container with bubblewrap")
    def test_an_isolating_runtime_passes_and_every_run_carries_the_hardening_flags(self) -> None:
        status = self.probe("isolated")
        self.assertTrue(status.available, status.reason)
        self.assertEqual(status.backend, "container")
        with mock.patch.dict(os.environ, {"STUB_MODE": "isolated"}):
            result = run_script(status, "print('hello from the container')")
        self.assertTrue(result.passed, result.stderr)
        run = [c for c in self.calls() if c[:1] == ["run"]][-1]
        joined = " ".join(run)
        for required in ("--rm", "--pull never", "--network none", "--read-only", "--cap-drop ALL",
                         "--security-opt no-new-privileges", "--pids-limit 128", "--user 65534:65534",
                         "--workdir /work", f"{self.IMAGE} python -s t.py"):
            self.assertIn(required, joined)
        self.assertIn("--memory", run)
        self.assertIn("--memory-swap", run)
        self.assertEqual(run.count("--mount"), 1)                       # exactly one thing is shared in
        mount = run[run.index("--mount") + 1]
        self.assertTrue(mount.startswith("type=bind,source=") and mount.endswith("target=/work"))
        self.assertNotIn(os.path.expanduser("~"), mount)
        for forbidden in ("--privileged", "-v", "--volume", "--network=host", "--pid", "--cap-add",
                          "--device", "--env-host", "--net"):
            self.assertNotIn(forbidden, run)
        self.assertFalse(any("KEY" in a or "TOKEN" in a for a in run))   # no credentials passed in

    @unittest.skipUnless(NATIVE.available and NATIVE.backend == "bubblewrap", "needs bubblewrap")
    def test_a_timed_out_container_is_removed_by_name(self) -> None:
        status = self.probe("isolated")
        self.assertTrue(status.available, status.reason)
        with mock.patch.dict(os.environ, {"STUB_MODE": "isolated"}):
            result = run_script(status, "import time\ntime.sleep(60)\n", check_timeout_s=5.0)
        self.assertTrue(result.timed_out)
        run = [c for c in self.calls() if c[:1] == ["run"]][-1]
        name = run[run.index("--name") + 1]
        self.assertIn(["rm", "-f", name], self.calls())

    def test_the_mount_argument_refuses_paths_it_cannot_express_safely(self) -> None:
        backend = sb._Container(self.stub, self.IMAGE)
        windows_path = r"C:\Users\Ana\AppData\Local\Temp\pybrowser-team-abc123"
        argv = backend.command(["python", "-s", "t.py"], windows_path, TeamLimits(), "n")
        self.assertIn(f"type=bind,source={windows_path},target=/work", argv)
        with self.assertRaises(RuntimeError):
            backend.command(["python"], r"C:\Users\a,b\Temp", TeamLimits(), "n")
        with self.assertRaises(RuntimeError):
            backend.command(["python"], 'C:\\Temp\\x",target=/etc', TeamLimits(), "n")


class WindowsPathAndWindowTests(unittest.TestCase):
    def test_bind_mount_survives_commas_in_the_path(self) -> None:
        self.assertEqual(sb._bind_mount("/tmp/x", "/work"), "type=bind,source=/tmp/x,target=/work")
        quoted = sb._bind_mount("C:\\Users\\Doe, Jane\\Temp", "/work")
        self.assertEqual(quoted, 'type=bind,"source=C:\\Users\\Doe, Jane\\Temp",target=/work')
        self.assertIn('""', sb._bind_mount('/a"b', "/work"))

    def test_docker_calls_do_not_open_a_console_window_on_windows(self) -> None:
        with mock.patch.object(sb.os, "name", "nt"):
            self.assertIn("creationflags", sb._Container("docker", "img").popen_kwargs(TeamLimits()))


if __name__ == "__main__":
    unittest.main()
