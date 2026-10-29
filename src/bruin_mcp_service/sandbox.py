"""
Run a command confined by Linux Landlock: an allow-list of folders and no TCP.

    python -I sandbox.py --ro DIR … --rw DIR … --rofile FILE … --rwfile FILE … -- CMD ARGS…

The launcher restricts ITSELF (landlock_restrict_self after PR_SET_NO_NEW_PRIVS)
and then execs CMD, which inherits the restriction and cannot lift it. It is
stdlib-only and run as a separate process, because restricting from inside the
multi-threaded server (or via subprocess preexec_fn) is unsafe.

Why Landlock: on this VM (Ubuntu 24.04, kernel 6.8) unprivileged user
namespaces are blocked by AppArmor (apparmor_restrict_unprivileged_userns=1)
and no bwrap/firejail/nsjail is installed, so namespace sandboxes need root.
Landlock needs neither. Verified 2026-10-23: ABI 4; a confined process could
read its allowed folder but got EACCES on /etc/passwd, on the service's token
file, and on an outbound TCP connect.

If Landlock is unavailable the launcher exits 97 WITHOUT running the command:
callers must treat that as "cannot run safely", never fall back to unconfined.
"""

import ctypes
import os
import sys

SYS_CREATE, SYS_ADD, SYS_RESTRICT = 444, 445, 446      # x86_64 syscall numbers
CREATE_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
EXIT_NO_LANDLOCK = 97

# filesystem rights (ABI 1–3)
EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE = 1 << 13, 1 << 14
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1          # ABI 4

FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE
RO = EXECUTE | READ_FILE | READ_DIR
RW = RO | WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG | MAKE_SYM | REFER | TRUNCATE

# what a dynamically linked program needs to start at all; read-only
SYSTEM_RO = ["/usr", "/lib", "/lib64", "/bin", "/proc/self", "/sys/devices/system/cpu",
             "/sys/kernel/mm/transparent_hugepage",
             "/sys/fs/cgroup"]        # DuckDB reads its memory limit here at start-up (verified)
SYSTEM_RO_FILES = ["/etc/ld.so.cache", "/etc/localtime", "/etc/nsswitch.conf", "/etc/passwd",
                   "/etc/group", "/etc/hosts", "/etc/resolv.conf", "/etc/gitconfig"]
SYSTEM_RW_FILES = ["/dev/null", "/dev/urandom"]


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def abi_version() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.syscall(SYS_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_VERSION))


def restrict(ro: list, rw: list, ro_files: list, rw_files: list) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = abi_version()
    if abi < 4:
        raise OSError(f"Landlock ABI {abi} is too old or unavailable (need 4 for network rules)")
    handled_fs = (1 << 15) - 1                        # every right ABI 3/4 knows about
    ruleset = _Ruleset(handled_fs, NET_BIND_TCP | NET_CONNECT_TCP)   # no TCP rules added → all denied
    fd = libc.syscall(SYS_CREATE, ctypes.byref(ruleset), ctypes.c_size_t(ctypes.sizeof(ruleset)),
                      ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

    def allow(path: str, access: int) -> None:
        if not os.path.exists(path):
            return
        if not os.path.isdir(path):
            access &= FILE_RIGHTS
        pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = _PathBeneath(access, pfd)
            if libc.syscall(SYS_ADD, ctypes.c_int(fd), ctypes.c_int(RULE_PATH_BENEATH),
                            ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
        finally:
            os.close(pfd)

    for path in SYSTEM_RO + list(ro):
        allow(path, RO)
    for path in SYSTEM_RO_FILES + list(ro_files):
        allow(path, EXECUTE | READ_FILE)
    for path in SYSTEM_RW_FILES + list(rw_files):
        allow(path, READ_FILE | WRITE_FILE | TRUNCATE)
    for path in rw:
        allow(path, RW)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")
    if libc.syscall(SYS_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    os.close(fd)


def command(launcher_python: str, cmd: list, ro=(), rw=(), ro_files=(), rw_files=()) -> list:
    """argv that runs `cmd` under this launcher."""
    argv = [launcher_python, "-I", os.path.abspath(__file__)]
    for flag, paths in (("--ro", ro), ("--rw", rw), ("--rofile", ro_files), ("--rwfile", rw_files)):
        for p in paths:
            argv += [flag, str(p)]
    return argv + ["--", *map(str, cmd)]


def main(argv: list) -> int:
    lists = {"--ro": [], "--rw": [], "--rofile": [], "--rwfile": []}
    i = 0
    while i < len(argv) and argv[i] != "--":
        flag = argv[i]
        if flag not in lists or i + 1 >= len(argv):
            print(f"sandbox: bad argument {flag!r}", file=sys.stderr)
            return 2
        lists[flag].append(argv[i + 1])
        i += 2
    cmd = argv[i + 1:]
    if not cmd:
        print("sandbox: no command", file=sys.stderr)
        return 2
    try:
        restrict(lists["--ro"], lists["--rw"], lists["--rofile"], lists["--rwfile"])
    except OSError as exc:
        print(f"sandbox: Landlock unavailable, refusing to run: {exc}", file=sys.stderr)
        return EXIT_NO_LANDLOCK
    os.execv(cmd[0], cmd)
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
