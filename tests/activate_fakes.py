"""A fake subprocess.run and a scratch DEPLOY/--root for activate.run_phase().

Shared by the activate hook's test files. The fake dispatches on argv, like
FakeApi dispatches on (method, url) in maintenance_fakes.py, and fails the
test loudly on any call it was not told to expect. activate.DEPLOY is
patched to a scratch directory for EVERY case: make test runs as root,
DEPLOY is hardcoded to the real /opt/nivuus/media-manager, and nothing here
may ever touch it.
"""
import io
import pathlib
import subprocess
import tempfile
from unittest import mock

VERSION = ("docker", "compose", "version")
CONFIG = ("docker", "compose", "config", "--services")
PS_A = ("docker", "compose", "ps", "-a", "--services")
CONFIG_XML = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<Config><ApiKey>{}</ApiKey></Config>\n')


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=list(argv), returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def fake_subprocess(routes, calls=None):
    """subprocess.run answering from routes; records each argv in calls."""
    def run(argv, **kwargs):
        key = tuple(argv)
        if calls is not None:
            calls.append(key)
        if key not in routes:
            raise AssertionError(f"unexpected subprocess call: {argv}")
        answer = routes[key]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return run


def timer_routes():
    """The systemctl calls of a run that reaches the timer arming."""
    routes = {("systemctl", "daemon-reload"): completed(("systemctl", "daemon-reload"))}
    for timer in ("media-manager-reset-error.timer", "media-manager-update-wanted.timer"):
        argv = ("systemctl", "start", timer)
        routes[argv] = completed(argv)
    return routes


def phase_runner(activate, repo):
    """run_phase_with(routes, deploy_files=None, inspect=None, calls=None)
    -> (exit code, stderr), for this loaded activate module.

    deploy_files pre-populates the scratch DEPLOY directory (relative path
    -> content), for a case that reaches the harvest/timers section — a
    failure case never does (it returns from the compose try/except first).
    Every timer unit install ships (systemd/*.timer) is pre-created in the
    scratch --root, as install leaves them: a success case's arm() calls
    have something to link to, and so would a timer armed by mistake.
    inspect, when given, is called with the scratch (deploy, root) paths
    after the phase and before both are removed, to read what it left.
    stdout is captured too (emit() prints progress there) so a failing case
    does not spam the test's own output — only the assertions should.
    """
    def run_phase_with(routes, deploy_files=None, inspect=None, calls=None):
        captured_stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as deploy, \
             tempfile.TemporaryDirectory() as root:
            for relpath, content in (deploy_files or {}).items():
                full = pathlib.Path(deploy) / relpath
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content)
            units_dir = pathlib.Path(root) / "etc/systemd/system"
            units_dir.mkdir(parents=True)
            for timer in (repo / "systemd").glob("*.timer"):
                (units_dir / timer.name).write_text("[Timer]\n")

            with mock.patch("subprocess.run", new=fake_subprocess(routes, calls)), \
                 mock.patch("sys.stderr", new=captured_stderr), \
                 mock.patch("sys.stdout", new=io.StringIO()), \
                 mock.patch.object(activate, "DEPLOY", deploy):
                code = activate.run_phase(root)
            if inspect is not None:
                inspect(pathlib.Path(deploy), pathlib.Path(root))
        return code, captured_stderr.getvalue()
    return run_phase_with
