# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Platforms.

Utilities dealing with platform specifics: signals, daemonization,
users, groups, and so on.
"""

import atexit
import errno
import numbers
import os
import platform as _platform
import signal as _signal
import sys
import threading
import warnings
from contextlib import contextmanager

from kombu.utils.encoding import safe_str

from .exceptions import SecurityError, SecurityWarning, reraise
from .local import try_import

_setproctitle = try_import("setproctitle")
resource = try_import("resource")
pwd = try_import("pwd")
grp = try_import("grp")


class _FakeProcess:
    """Fake process object for threading-based execution."""

    _name: str | None = None

    @property
    def name(self) -> str:
        return self._name or f"Thread-{threading.current_thread().ident}"

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def ident(self) -> int:
        return os.getpid()

    @property
    def pid(self) -> int:
        return self.ident


_current_process: _FakeProcess | None = None


def current_process() -> _FakeProcess:
    """Return the current process object."""
    global _current_process
    if _current_process is None:
        _current_process = _FakeProcess()
    return _current_process


__all__ = (
    "EX_OK",
    "EX_FAILURE",
    "EX_UNAVAILABLE",
    "SYSTEM",
    "IS_macOS",
    "IS_WINDOWS",
    "LockFailed",
    "Pidfile",
    "create_pidlock",
    "close_open_fds",
    "detached",
    "maybe_drop_privileges",
    "signals",
    "set_process_title",
    "set_mp_process_title",
    "ignore_errno",
    "isatty",
    "check_privileges",
)

# exitcodes
EX_OK = getattr(os, "EX_OK", 0)
EX_FAILURE = 1
EX_UNAVAILABLE = getattr(os, "EX_UNAVAILABLE", 69)
EX_CANTCREAT = getattr(os, "EX_CANTCREAT", 73)

SYSTEM = _platform.system()
IS_macOS = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows"

PIDFILE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY
PIDFILE_MODE = ((os.R_OK | os.W_OK) << 6) | ((os.R_OK) << 3) | (os.R_OK)

PIDLOCKED = """ERROR: Pidfile ({0}) already exists.
Seems we're already running? (pid: {1})"""

ROOT_DISALLOWED = """\
Running a worker with superuser privileges when the
worker accepts messages serialized with pickle is a very bad idea!

If you really want to continue then you have to set the C_FORCE_ROOT
environment variable (but please think about this before you do).

User information: uid={uid} euid={euid} gid={gid} egid={egid}
"""

ROOT_DISCOURAGED = """\
You're running the worker with superuser privileges: this is
absolutely not recommended!

Please specify a different user using the --uid option.

User information: uid={uid} euid={euid} gid={gid} egid={egid}
"""

ASSUMING_ROOT = """\
An entry for the specified gid or egid was not found.
We're assuming this is a potential security issue.
"""


def isatty(fh):
    """Return true if the process has a controlling terminal."""
    try:
        return fh.isatty()
    except AttributeError:
        pass


def pyimplementation():
    """Return string identifying the current Python implementation."""
    return _platform.python_implementation()


class LockFailed(Exception):
    """Raised if a PID lock can't be acquired."""


class Pidfile:
    """Pidfile.

    This is the type returned by :func:`create_pidlock`.

    See Also:
        Best practice is to not use this directly but rather use
        the :func:`create_pidlock` function instead:
        more convenient and also removes stale pidfiles (when
        the process holding the lock is no longer running).
    """

    path = None

    def __init__(self, path):
        self.path = os.path.abspath(path)

    def acquire(self):
        """Acquire lock."""
        try:
            self.write_pid()
        except OSError as exc:
            reraise(LockFailed, LockFailed(str(exc)), sys.exc_info()[2])
        return self

    __enter__ = acquire

    def is_locked(self):
        """Return true if the pid lock exists."""
        return os.path.exists(self.path)

    def release(self, *args):
        """Release lock."""
        self.remove()

    __exit__ = release

    def read_pid(self):
        """Read and return the current pid."""
        with ignore_errno("ENOENT"), open(self.path) as fh:
            line = fh.readline()
            if line.strip() == line:  # must contain '\n'
                raise ValueError(f"Partial or invalid pidfile {self.path}")
            try:
                return int(line.strip())
            except ValueError:
                raise ValueError(f"pidfile {self.path} contents invalid.")

    def remove(self):
        """Remove the lock."""
        with ignore_errno(errno.ENOENT, errno.EACCES):
            os.unlink(self.path)

    def remove_if_stale(self):
        """Remove the lock if the process isn't running."""
        try:
            pid = self.read_pid()
        except ValueError:
            print("Broken pidfile found - Removing it.", file=sys.stderr)
            self.remove()
            return True
        if not pid:
            self.remove()
            return True
        if pid == os.getpid():
            self.remove()
            return True
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH or exc.errno == errno.EPERM:
                print("Stale pidfile exists - Removing it.", file=sys.stderr)
                self.remove()
                return True
        except SystemError:
            print("Stale pidfile exists - Removing it.", file=sys.stderr)
            self.remove()
            return True
        return False

    def write_pid(self):
        pid = os.getpid()
        content = f"{pid}\n"
        pidfile_fd = os.open(self.path, PIDFILE_FLAGS, PIDFILE_MODE)
        pidfile = os.fdopen(pidfile_fd, "w")
        try:
            pidfile.write(content)
            pidfile.flush()
            try:
                os.fsync(pidfile_fd)
            except AttributeError:
                pass
        finally:
            pidfile.close()
        rfh = open(self.path)
        try:
            if rfh.read() != content:
                raise LockFailed("Inconsistency: Pidfile content doesn't match at re-read")
        finally:
            rfh.close()


def create_pidlock(pidfile):
    """Create and verify pidfile.

    If the pidfile already exists the program exits with an error message,
    however if the process it refers to isn't running anymore, the pidfile
    is deleted and the program continues.

    Automatically installs an :mod:`atexit` handler to release the lock.
    """
    pidlock = _create_pidlock(pidfile)
    atexit.register(pidlock.release)
    return pidlock


def _create_pidlock(pidfile):
    pidlock = Pidfile(pidfile)
    if pidlock.is_locked() and not pidlock.remove_if_stale():
        print(PIDLOCKED.format(pidfile, pidlock.read_pid()), file=sys.stderr)
        raise SystemExit(EX_CANTCREAT)
    pidlock.acquire()
    return pidlock


def get_fdmax(default=1024):
    """Return the maximum file descriptor number."""
    try:
        fdmax = os.sysconf("SC_OPEN_MAX")
    except AttributeError, KeyError, ValueError:
        fdmax = None
    if fdmax is None and resource:
        try:
            _soft, fdmax = resource.getrlimit(resource.RLIMIT_NOFILE)
        except ValueError, resource.error:
            fdmax = None
    if fdmax is None or (resource and fdmax == resource.RLIM_INFINITY):
        return default
    return fdmax


def close_open_fds(keep=None):
    """Close all open file descriptors except those in keep."""
    keep = keep or set()
    keep.update({0, 1, 2})
    fdmax = get_fdmax()
    for fd in range(3, fdmax):
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def fd_by_path(paths):
    """Return file descriptors corresponding to file paths."""
    stats = set()
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            stats.add(os.fstat(fd)[1:3])
        finally:
            os.close(fd)

    def fd_in_stats(fd):
        try:
            return os.fstat(fd)[1:3] in stats
        except OSError:
            return False

    return [_fd for _fd in range(get_fdmax(2048)) if fd_in_stats(_fd)]


class DaemonContext:
    """Context manager daemonizing the process."""

    _is_open = False

    def __init__(self, pidfile=None, workdir=None, umask=None, fake=False, after_chdir=None, **kwargs):
        if isinstance(umask, str):
            umask = int(umask, 8 if umask.startswith("0") else 10)
        self.workdir = workdir or "/"
        self.umask = umask
        self.fake = fake
        self.after_chdir = after_chdir
        self.stdfds = (sys.stdin, sys.stdout, sys.stderr)

    def open(self):
        if not self._is_open:
            if not self.fake:
                self._detach()
            os.chdir(self.workdir)
            if self.umask is not None:
                os.umask(self.umask)
            if self.after_chdir:
                self.after_chdir()
            if not self.fake:
                from kombu.utils.compat import maybe_fileno

                keep = list(self.stdfds) + fd_by_path(["/dev/urandom"])
                close_open_fds(keep)
                for fd in self.stdfds:
                    fno = maybe_fileno(fd)
                    if fno is not None:
                        dest = os.open(os.devnull, os.O_RDWR)
                        os.dup2(dest, fno)
            self._is_open = True

    __enter__ = open

    def close(self, *args):
        if self._is_open:
            self._is_open = False

    __exit__ = close

    def _detach(self):
        if os.fork() == 0:
            os.setsid()
            if os.fork() > 0:
                os._exit(0)
        else:
            os._exit(0)
        return self


def detached(logfile=None, pidfile=None, uid=None, gid=None, umask=0, workdir=None, fake=False, **opts):
    """Detach the current process in the background (daemonize)."""
    if not resource:
        raise RuntimeError("This platform does not support detach.")
    workdir = os.getcwd() if workdir is None else workdir

    signals.reset("SIGCLD")
    maybe_drop_privileges(uid=uid, gid=gid)

    def after_chdir_do():
        logfile and open(logfile, "a").close()
        if pidfile:
            _create_pidlock(pidfile).release()

    return DaemonContext(umask=umask, workdir=workdir, fake=fake, after_chdir=after_chdir_do)


def parse_uid(uid):
    """Parse user id (int or username string)."""
    try:
        return int(uid)
    except ValueError:
        try:
            return pwd.getpwnam(uid).pw_uid
        except AttributeError, KeyError:
            raise KeyError(f"User does not exist: {uid}")


def parse_gid(gid):
    """Parse group id (int or group name string)."""
    try:
        return int(gid)
    except ValueError:
        try:
            return grp.getgrnam(gid).gr_gid
        except AttributeError, KeyError:
            raise KeyError(f"Group does not exist: {gid}")


def maybe_drop_privileges(uid=None, gid=None):
    """Change process privileges to new user/group."""
    if os.geteuid():
        if not os.getuid():
            raise SecurityError("contact support")
    uid = uid and parse_uid(uid)
    gid = gid and parse_gid(gid)

    if uid:
        if not gid and pwd:
            gid = pwd.getpwuid(uid).pw_gid
        os.setgid(parse_gid(gid) if isinstance(gid, str) else gid)
        if pwd:
            pw = pwd.getpwuid(uid)
            username = pw.pw_name if hasattr(pw, "pw_name") else pw[0]
            os.initgroups(username, gid)
        os.setuid(uid)
        try:
            os.setuid(0)
        except OSError as exc:
            if exc.errno != errno.EPERM:
                raise
        else:
            raise SecurityError("non-root user able to restore privileges after setuid.")
    elif gid:
        os.setgid(parse_gid(gid) if isinstance(gid, str) else gid)

    if uid and not os.getuid() and not os.geteuid():
        raise SecurityError("Still root uid after drop privileges!")
    if gid and not os.getgid() and not os.getegid():
        raise SecurityError("Still root gid after drop privileges!")


class Signals:
    """Convenience interface to :mod:`signal`.

    If the requested signal isn't supported on the current platform,
    the operation will be ignored.
    """

    ignored = _signal.SIG_IGN
    default = _signal.SIG_DFL

    def arm_alarm(self, seconds):
        return _signal.setitimer(_signal.ITIMER_REAL, seconds)

    def reset_alarm(self):
        return _signal.alarm(0)

    def supported(self, name):
        """Return true if signal by ``name`` exists on this platform."""
        try:
            self.signum(name)
        except AttributeError:
            return False
        return True

    def signum(self, name):
        """Get signal number by name."""
        if isinstance(name, numbers.Integral):
            return name
        if not isinstance(name, str) or not name.isupper():
            raise TypeError("signal name must be uppercase string.")
        if not name.startswith("SIG"):
            name = "SIG" + name
        return getattr(_signal, name)

    def reset(self, *signal_names):
        """Reset signals to the default signal handler."""
        self.update((sig, self.default) for sig in signal_names)

    def ignore(self, *names):
        """Ignore signal using :const:`SIG_IGN`."""
        self.update((sig, self.ignored) for sig in names)

    def __getitem__(self, name):
        return _signal.getsignal(self.signum(name))

    def __setitem__(self, name, handler):
        """Install signal handler."""
        try:
            _signal.signal(self.signum(name), handler)
        except AttributeError, ValueError:
            pass

    def update(self, _d_=None, **sigmap):
        """Set signal handlers from a mapping."""
        for name, handler in dict(_d_ or {}, **sigmap).items():
            self[name] = handler


signals = Signals()


def strargv(argv):
    arg_start = 2 if "manage" in argv[0] else 1
    if len(argv) > arg_start:
        return " ".join(argv[arg_start:])
    return ""


def set_process_title(progname, info=None):
    """Set the :command:`ps` name for the currently running process."""
    proctitle = f"[{progname}]"
    proctitle = f"{proctitle} {info}" if info else proctitle
    if _setproctitle:
        _setproctitle.setproctitle(safe_str(proctitle))
    return proctitle


def set_mp_process_title(progname, info=None, hostname=None):
    """Set the :command:`ps` name from the current process name."""
    if hostname:
        progname = f"{progname}: {hostname}"
    return set_process_title(f"{progname}:MainProcess", info=info)


def get_errno_name(n):
    """Get errno for string (e.g., ``ENOENT``)."""
    if isinstance(n, str):
        return getattr(errno, n)
    return n


@contextmanager
def ignore_errno(*errnos, **kwargs):
    """Context manager to ignore specific POSIX error codes."""
    types = kwargs.get("types") or (Exception,)
    errnos = [get_errno_name(errno) for errno in errnos]
    try:
        yield
    except types as exc:
        if not hasattr(exc, "errno"):
            raise
        if exc.errno not in errnos:
            raise


def check_privileges(accept_content):
    """Check if running as root with pickle serializer enabled."""
    if grp is None or pwd is None:
        return
    pickle_or_serialize = "pickle" in accept_content or "application/group-python-serialize" in accept_content

    uid = os.getuid()
    gid = os.getgid()
    euid = os.geteuid()
    egid = os.getegid()

    try:
        gid_entry = grp.getgrgid(gid)
        egid_entry = grp.getgrgid(egid)
    except KeyError:
        warnings.warn(SecurityWarning(ASSUMING_ROOT), stacklevel=2)
        _warn_or_raise_security_error(egid, euid, gid, uid, pickle_or_serialize)
        return

    gids_in_use = (gid_entry[0], egid_entry[0])
    groups_with_security_risk = ("sudo", "wheel")

    is_root = uid == 0 or euid == 0
    if is_root or any(group in gids_in_use for group in groups_with_security_risk):
        _warn_or_raise_security_error(egid, euid, gid, uid, pickle_or_serialize)


def _warn_or_raise_security_error(egid, euid, gid, uid, pickle_or_serialize):
    c_force_root = os.environ.get("C_FORCE_ROOT", False)
    if pickle_or_serialize and not c_force_root:
        raise SecurityError(ROOT_DISALLOWED.format(uid=uid, euid=euid, gid=gid, egid=egid))
    warnings.warn(
        SecurityWarning(ROOT_DISCOURAGED.format(uid=uid, euid=euid, gid=gid, egid=egid)),
        stacklevel=2,
    )
