# PlatformIO pre-script: embed the short commit SHA in the firmware build so
# `$I` identifies the binary that is actually flashed (finding F-016).
#
# Contract: runs before the build, defines GRBL_VERSION_BUILD as a C string
# literal. No third-party dependencies (stdlib only); no files are written.

Import("env")  # noqa: F821 - provided by SCons/PlatformIO

import os
import subprocess

FALLBACK = "local"  # no git metadata (source tarball, CI export)
MACRO = "GRBL_VERSION_BUILD"


def _git(project_dir, *args):
    """Run git and return stripped stdout, or None when git is unavailable."""
    try:
        output = subprocess.check_output(
            ("git",) + args,
            cwd=project_dir,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return output.decode("utf-8", "replace").strip()


def _own_repo(project_dir):
    """True only when project_dir itself is the root of its git repository."""
    top = _git(project_dir, "rev-parse", "--show-toplevel")
    if not top:
        return False
    # A copy of the sources inside someone else's repo must not borrow its SHA.
    return os.path.realpath(top) == os.path.realpath(project_dir)


def build_id(project_dir):
    """Short SHA of HEAD, with a -dirty suffix for modified tracked files."""
    if not _own_repo(project_dir):
        return FALLBACK
    sha = _git(project_dir, "rev-parse", "--short=7", "HEAD")
    if not sha:
        return FALLBACK
    # Untracked files are ignored on purpose: .pio/ and scratch files are not
    # part of the source that produced the binary.
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=no")
    if status is None:
        return FALLBACK
    if status:
        sha += "-dirty"
    return sha


_value = build_id(env.subst("$PROJECT_DIR"))
# Double quotes must survive the SCons variable expansion and reach the
# compiler as \"...\" so the macro expands to a string literal.
env.Append(CPPDEFINES=[(MACRO, '\\"%s\\"' % _value)])
print("[build_hash] %s=%s" % (MACRO, _value))
