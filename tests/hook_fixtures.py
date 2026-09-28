"""Fixtures for the tests of the install hook: its context, a target root, fake packages.

install.py reads the engine's context as JSON on stdin and writes under
--root. A fake package is a self-contained copy of this one (its own
hooks/, systemd/ and a tiny stack/), so that when its own install.py runs,
HERE resolves inside it and the real repository is never touched. Not a
test itself: the Makefile only runs the test_* files. Import it after
setting sys.dont_write_bytecode, so that no __pycache__ lands in tests/.
"""
import json
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

# The wizard's answers, as the engine passes them to the install hook.
ANSWERS = {
    "media_root": "/srv/media",
    "transcode_dir": "/srv/transcode",
    "timezone": "Europe/Paris",
    "nvenc_node": True,
    "usenet": False,
    "plex_claim": "claim-abc",
}


def context(answers):
    """The engine's context for this package, as the hook reads it on stdin."""
    return json.dumps({
        "package": {"name": "media-manager", "version": "1.0.0",
                    "root": str(REPO)},
        "hw": {"gpus": [{"slot": "00:02.0", "vendor": "intel",
                         "discrete": False}]},
        "answers": answers,
    })


def fake_group_file(root):
    """Une cible ou render vaut 106, pas la valeur par defaut 105."""
    path = pathlib.Path(root) / "etc" / "group"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("root:x:0:\nvideo:x:44:\nrender:x:106:\n")


def make_stack_skeleton(pkg):
    """A minimal fake package directory: hooks/, systemd/, and a tiny stack/."""
    shutil.copytree(REPO / "hooks", pkg / "hooks")
    shutil.copytree(REPO / "systemd", pkg / "systemd")
    (pkg / "stack").mkdir()
    shutil.copy2(REPO / "stack" / "env.template", pkg / "stack" / "env.template")
    (pkg / "stack" / "docker-compose.yml").write_text("services: {}\n")


def run_pkg(pkg, root, env=None):
    """Run a fake package's own install.py against --root root."""
    return subprocess.run(
        [sys.executable, str(pkg / "hooks" / "install.py"),
         "--phase", "install", "--root", root],
        input=context(ANSWERS), capture_output=True, text=True, cwd=str(pkg),
        env=env)


def git_commit_tracked(pkg, *tracked_paths):
    """git init + add + commit exactly the given paths, inside pkg."""
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=str(pkg),
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    git("init", "-q")
    git("add", *tracked_paths)
    git("-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "tracked files")
