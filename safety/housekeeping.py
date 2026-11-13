"""Daemon start-up housekeeping: the single-instance lock and the sweep of temp files that
a killed earlier run left behind.

Both run at the very top of server.main(), before any layer, thread or file is touched.

* ``run_lock``: an exclusive, non-blocking flock on a private lock file. Only "another
  process holds the lock" (EWOULDBLOCK / EAGAIN) stops the daemon (AlreadyRunning); every
  other failure — the file is a link, someone else's, on a mount without flock — is a
  LockError, which main() logs as a warning and then runs unguarded: a safety daemon
  should rather run without the guard than not at all.
* ``sweep_leftovers``: removes this program's own temp files that a SIGKILL / OOM kill /
  power cut left behind (regular files owned by this user, older than an hour, matching
  only the program's own names). Never follows a link; never raises.
"""
from __future__ import annotations

import errno
import fcntl
import fnmatch
import logging
import os
import stat
import time
from typing import List, Optional

log = logging.getLogger("ttu.safety")

LOCK_MODE = 0o600       # flock needs only an open descriptor: nobody else may open the file

# Where the RAM-disk temp files live: the GLM netCDF fallback (glm_lightning, always
# /dev/shm) and the page's files (make_status_page.py: _SHM, and status.html in /dev/shm).
# Computed as config._SHM and make_status_page._SHM are; a module constant the tests point
# at a private directory.
SHM_DIR = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"

# A temp file younger than this may still be in use (a page run writing its .tmp right
# now, a GLM granule being parsed); a killed run's leftovers are only ever older.
SWEEP_MIN_AGE_SEC = 3600.0


class AlreadyRunning(Exception):
    """Another process holds the run lock. ``pid``: the pid it recorded, or None."""

    def __init__(self, msg: str = "", pid: Optional[int] = None):
        super().__init__(msg)
        self.pid = pid


class LockError(OSError):
    """The run lock cannot be taken for another reason than a run holding it (the lock
    file is a link, not a regular file, someone else's, or flock itself fails: NFS without
    a lock service answers ENOLCK, some FUSE mounts ENOSYS). Never "already running"."""


def _recorded_pid(fd: int) -> Optional[int]:
    """The pid the lock holder wrote into the file, or None if none can be read."""
    try:
        first = os.pread(fd, 64, 0).split(b"\n", 1)[0].split()
        pid = int(first[0]) if first else 0
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def run_lock(path: str) -> int:
    """Take the single-instance lock on ``path`` and return its descriptor, which the
    caller keeps open for the life of the process (closing it releases the lock; so does
    the process's exit, however it dies).

    The file is opened with O_NOFOLLOW (a planted symbolic link is refused, not followed)
    and O_CLOEXEC (a child — the status page run — never inherits the lock), created mode
    0600, and must be a regular file owned by this user; a wider mode is narrowed. Raises
    AlreadyRunning only when flock answers EWOULDBLOCK / EAGAIN, and LockError for every
    other failure. Once taken, the file holds "<pid> <UTC start time>": rewritten in place
    at each start, the file itself is created only once."""
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(path, flags, LOCK_MODE)
    except OSError as e:
        why = "it is a symbolic link" if e.errno == errno.ELOOP else (e.strerror or str(e))
        raise LockError(e.errno, "cannot open the lock file %s: %s" % (path, why)) from e
    try:
        os.set_inheritable(fd, False)        # (os.open already does; be explicit)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise LockError(errno.EPERM, "the lock file %s is not a regular file owned by "
                                         "this user (uid %d); remove it"
                            % (path, os.geteuid()))
        if st.st_mode & 0o077:
            try:
                os.fchmod(fd, LOCK_MODE)
                log.info("lock file %s: mode %03o narrowed to %03o", path,
                         st.st_mode & 0o777, LOCK_MODE)
            except OSError as e:        # a mount without modes: the lock still works
                log.warning("lock file %s: mode %03o could not be narrowed (%s)", path,
                            st.st_mode & 0o777, e)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise AlreadyRunning("another instance holds %s" % path, _recorded_pid(fd))
            raise LockError(e.errno, "cannot lock %s: %s" % (path, e.strerror or e)) from e
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, ("%d %s\n" % (os.getpid(), time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))).encode("ascii"), 0)
        except OSError as e:            # the lock is held all the same
            log.warning("lock file %s: cannot record the pid (%s)", path, e)
        return fd
    except BaseException:
        os.close(fd)
        raise


def release_lock(fd: Optional[int]) -> None:
    """Release and close a descriptor from run_lock (None: nothing to do). The file stays,
    so the next start does not create it again."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _leftover_patterns(cfg) -> List[tuple]:
    """(directory, glob) pairs: this program's own temp-file names, nothing else."""
    state_dir = os.path.dirname(cfg.STATE_FILE) or "."
    inputs = cfg.INPUTS_FILE
    return [
        # glm_lightning's netCDF fallback (mkstemp, prefix "ttu-glm-") in the RAM disk
        (SHM_DIR, "ttu-glm-*.nc"),
        # monitor._write_state's unique temp files (mkstemp beside the state file)
        (state_dir, ".safety_state.*.tmp"),
        # make_status_page.py's fixed "<file>.tmp" names: the inputs it hands the daemon,
        # its own cache, and the page itself (written in /dev/shm behind the web-root link)
        (os.path.dirname(inputs) or ".", os.path.basename(inputs) + ".tmp"),
        (SHM_DIR, "status_page_cache.json.tmp"),
        (SHM_DIR, "status.html.tmp"),
    ]


def sweep_leftovers(cfg, now: Optional[float] = None,
                    min_age_s: float = SWEEP_MIN_AGE_SEC) -> List[str]:
    """Remove the temp files a killed earlier run left behind; return the paths removed.

    Only regular files (lstat: a link is never followed nor removed), owned by this user,
    last modified more than ``min_age_s`` ago, and named like one of this program's own
    temp files (``_leftover_patterns``). Each removal is logged. Never raises."""
    if now is None:
        now = time.time()
    uid = os.geteuid()
    removed = []
    seen = set()
    for directory, pattern in _leftover_patterns(cfg):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not fnmatch.fnmatchcase(name, pattern):
                continue
            path = os.path.join(directory, name)
            if path in seen:
                continue
            seen.add(path)
            try:
                st = os.lstat(path)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid
                        or now - st.st_mtime <= min_age_s):
                    continue
                os.unlink(path)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    log.warning("cannot remove the leftover temp file %s: %s", path, e)
                continue
            removed.append(path)
            log.info("removed a temp file left by a killed run: %s (%d bytes, %.1f h old)",
                     path, st.st_size, (now - st.st_mtime) / 3600.0)
    return removed
