#!/usr/bin/env python3
"""hooks/safe_copy.py: git-tracked-only deployment, symlink-hardened.

Verified by ARTIFACTS under a temporary root, through install.py's own
subprocess invocation — never by simulating the copy, since what matters is
what actually lands on the target filesystem. Every case here uses a
SEPARATE, self-contained fake package directory (its own hooks/, systemd/,
stack/) so HERE resolves inside it when its own copy of install.py runs, and
the real repository is never touched.

Three concerns, all in copy_stack(): a checkout deploys only what git
tracks (rule 4 of install.py's docstring) while a git-archive export (no
.git) deploys the whole tree; a .env is never copied in either mode; and
every write is hardened against a symlink planted by a container with a
read-write bind mount under the deploy tree (Tdarr's
tdarr/server/Tdarr/Plugins/..., see stack/docker-compose.yml) — checked on
both the source and the destination side.

Run: python3 tests/test_safe_copy.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
DEST_REL = "opt/nivuus/media-manager"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# Same shape as tests/test_install_hook.py's own helpers: each test file
# here is standalone (no test-to-test imports, which would re-run the other
# file's whole suite as an import side effect — these are scripts, not
# guarded modules).
ANSWERS = {
    "media_root": "/srv/media",
    "transcode_dir": "/srv/transcode",
    "timezone": "Europe/Paris",
    "nvenc_node": True,
    "usenet": False,
    "plex_claim": "claim-abc",
}


def context(answers):
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


def build_git_checkout(pkg):
    make_stack_skeleton(pkg)
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml")

    # Untracked: must never reach the deployment.
    (pkg / "stack" / ".env").write_text("RADARR_API_KEY=leaked-untracked-key\n")
    (pkg / "stack" / "radarr").mkdir()
    (pkg / "stack" / "radarr" / "config.xml").write_text("<Config/>\n")


# --- A git checkout deploys only what git tracks under stack/ -------------
# build.sh exports via `git archive HEAD`, so an export already went through
# the commit filter and is copied as-is. A checkout has not: copying the
# whole subtree would ship whatever sits there uncommitted, most often a
# real .env or a service's runtime config.xml.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    build_git_checkout(pkg)
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("checkout: exit status", proc.returncode, 0)

    dest = pathlib.Path(root) / DEST_REL
    check("checkout: tracked file deployed",
          (dest / "docker-compose.yml").is_file(), True)
    check("checkout: untracked config.xml not deployed",
          (dest / "radarr" / "config.xml").exists(), False)
    # The rendered .env is always written; the proof the untracked stack/.env
    # was never copied (and therefore never merge_env()-preserved) is that
    # its content never reaches the deployed one.
    check("checkout: untracked .env content not deployed",
          "leaked-untracked-key" in (dest / ".env").read_text(), False)

# --- A git-archive export (no .git) still deploys the whole tree ----------
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    # No .git anywhere: this is what `git archive HEAD | tar -x` produces.
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("archive export: exit status", proc.returncode, 0)
    dest = pathlib.Path(root) / DEST_REL
    check("archive export: file deployed",
          (dest / "docker-compose.yml").is_file(), True)

# --- Never copy a .env, in EITHER mode (fix round 1, item 2) ---------------
# Neither mode excluded it before: a force-added stack/.env (checkout) or a
# stack/.env sitting in an export's working tree overwrote the live .env in
# scratch runs, before merge_env() ever got to read it.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / ".env").write_text("RADARR_API_KEY=leaked-force-added\n")
    git_commit_tracked(pkg, "-f", "stack/env.template", "stack/docker-compose.yml",
                       "stack/.env")

    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    (dest / ".env").write_text("RADARR_API_KEY=real-production-key\n")
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("force-added .env: exit status", proc.returncode, 0)
    check("force-added .env: pre-existing key kept",
          "real-production-key" in (dest / ".env").read_text(), True)
    check("force-added .env: leaked content never arrives",
          "leaked-force-added" in (dest / ".env").read_text(), False)

with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / ".env").write_text("RADARR_API_KEY=leaked-export\n")
    # No .git anywhere: an export, exactly like the archive-export case above.

    dest = pathlib.Path(root) / DEST_REL
    dest.mkdir(parents=True)
    (dest / ".env").write_text("RADARR_API_KEY=real-production-key\n")
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("export .env: exit status", proc.returncode, 0)
    check("export .env: pre-existing key kept",
          "real-production-key" in (dest / ".env").read_text(), True)
    check("export .env: leaked content never arrives",
          "leaked-export" in (dest / ".env").read_text(), False)

# --- A tracked file missing from the working tree fails cleanly and deploys
# NOTHING (fix round 1, item 6) ---------------------------------------------
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "zzz-marker.txt").write_text("marker\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/zzz-marker.txt")

    # Deleted after the commit, and sorting AFTER the other two tracked
    # files (git ls-files is lexicographic: d < e < z): without a pre-flight
    # existence check, docker-compose.yml and env.template would already be
    # copied by the time this one is found missing, leaving a PARTIAL
    # deploy behind.
    (pkg / "stack" / "zzz-marker.txt").unlink()
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("missing tracked file: exit status", proc.returncode, 1)
    check("missing tracked file: clean message",
          proc.stderr.startswith("media-manager install:"), True)
    check("missing tracked file: file named",
          "zzz-marker.txt" in proc.stderr, True)
    check("missing tracked file: no traceback", "Traceback" in proc.stderr, False)
    check("missing tracked file: nothing deployed at all",
          (pathlib.Path(root) / DEST_REL).exists(), False)

# --- A missing git binary maps to the same clean error path (item 6) ------
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    build_git_checkout(pkg)  # a real checkout, so install.py WILL try git
    fake_group_file(root)

    proc = run_pkg(pkg, root, env={"PATH": "/nonexistent"})
    check("git binary missing: exit status", proc.returncode, 1)
    check("git binary missing: clean message",
          proc.stderr.startswith("media-manager install:"), True)
    check("git binary missing: no traceback", "Traceback" in proc.stderr, False)
    check("git binary missing: nothing deployed",
          (pathlib.Path(root) / DEST_REL).exists(), False)

# --- Root writes never follow symlinks: a hijacked ancestor directory -----
# (fix round 1, item 7). A container with a read-write bind mount under the
# deploy tree (Tdarr's tdarr/server/Tdarr/Plugins/..., see
# stack/docker-compose.yml) could plant a symlink ahead of a reinstall,
# redirecting a root-run write anywhere else on the host.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tdarr" / "server").mkdir(parents=True)
    (pkg / "stack" / "tdarr" / "server" / "marker.txt").write_text(
        "expected content\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tdarr/server/marker.txt")

    dest = pathlib.Path(root) / DEST_REL
    (dest / "tdarr").mkdir(parents=True)
    victim = pathlib.Path(root) / "victim"
    victim.mkdir()
    (dest / "tdarr" / "server").symlink_to(victim)
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("symlinked ancestor: exit status", proc.returncode, 1)
    check("symlinked ancestor: clean message",
          proc.stderr.startswith("media-manager install:"), True)
    check("symlinked ancestor: no traceback", "Traceback" in proc.stderr, False)
    check("symlinked ancestor: victim untouched",
          sorted(p.name for p in victim.iterdir()), [])
    check("symlinked ancestor: symlink itself untouched",
          (dest / "tdarr" / "server").is_symlink(), True)

# --- Root writes never follow symlinks: a hijacked destination file -------
# The final path component is replaced (os.replace), never opened and
# written through: the symlink's target must survive exactly as it was.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / "stack" / "tdarr" / "server").mkdir(parents=True)
    (pkg / "stack" / "tdarr" / "server" / "marker.txt").write_text(
        "expected content\n")
    git_commit_tracked(pkg, "stack/env.template", "stack/docker-compose.yml",
                       "stack/tdarr/server/marker.txt")

    dest = pathlib.Path(root) / DEST_REL
    (dest / "tdarr" / "server").mkdir(parents=True)
    victim = pathlib.Path(root) / "victim.txt"
    victim.write_text("original victim content\n")
    (dest / "tdarr" / "server" / "marker.txt").symlink_to(victim)
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("symlinked destination file: exit status", proc.returncode, 0)
    check("symlinked destination file: replaced with a regular file",
          (dest / "tdarr" / "server" / "marker.txt").is_symlink(), False)
    check("symlinked destination file: tracked content written",
          (dest / "tdarr" / "server" / "marker.txt").read_text(),
          "expected content\n")
    check("symlinked destination file: victim untouched",
          victim.read_text(), "original victim content\n")

# --- A failing git command fails the install loudly, never a fallback -----
# Ruling 18: git 2.47 on the reference host refuses a repository it does not
# own when run as root ("dubious ownership"). Whatever the reason, a
# non-zero exit from `git ls-files` must never be read as "not a checkout"
# (that fallback is exactly what would ship an untracked .env or
# config.xml) — it must fail the phase with nothing deployed at all. A `.git`
# that exists but is not a valid repository pointer reproduces a real
# `git ls-files` failure without mocking anything.
with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as root:
    pkg = pathlib.Path(fake_pkg)
    make_stack_skeleton(pkg)
    (pkg / ".git").write_text("not a real git repository\n")
    fake_group_file(root)

    proc = run_pkg(pkg, root)
    check("broken git: exit status", proc.returncode, 1)
    check("broken git: command named", "ls-files" in proc.stderr, True)
    check("broken git: no fallback, nothing deployed at all",
          (pathlib.Path(root) / DEST_REL).exists(), False)

# --- The git command's exact argv is pinned (Ruling 18) --------------------
# git 2.47 on the reference host refuses a repository it does not own when
# run as root ("detected dubious ownership": the checkout is the operator's,
# install runs as root), unless safe.directory names it explicitly. The real
# git binary still runs here (a transparent spy, not a mock) — only the argv
# it was called with is pinned.
sys.path.insert(0, str(REPO / "hooks"))
import safe_copy  # noqa: E402

_real_run = subprocess.run
_git_calls = []


def _spy_run(argv, **kwargs):
    _git_calls.append(list(argv))
    return _real_run(argv, **kwargs)


with mock.patch("subprocess.run", new=_spy_run):
    safe_copy.git_tracked_stack_files(str(REPO))

check("git argv pinned", _git_calls, [
    ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
     "ls-files", "-z", "stack/"],
])

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_safe_copy: OK")
