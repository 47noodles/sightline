"""Cross-process lock for anything that touches the live desktop.

Several agents and test runs share one Windows session.  Anything that moves
the real mouse, types real keys, opens windows that steal focus, or takes a
screenshot must hold this lock so runs do not trample each other.

Python::

    from tools.desktop_lock import desktop_lock
    with desktop_lock("speech-e2e"):
        ...

Shell::

    python tools/desktop_lock.py run <owner> -- <command> [args...]
    python tools/desktop_lock.py status

The lock is an OS file lock, so it is released automatically if the holder dies.

A second lock, ``name="cpu"``, serialises latency benchmarks so four agents
measuring at once do not distort each other's numbers::

    with desktop_lock("speech-bench", name="cpu"):
        ...
    python tools/desktop_lock.py run speech-bench --name cpu -- python tools/speech_bench.py
"""

from __future__ import annotations

import contextlib
import msvcrt
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path


def _lock_paths(name: str) -> tuple[Path, Path]:
    base = Path(tempfile.gettempdir())
    return base / f"sightline-{name}.lock", base / f"sightline-{name}.owner"


@contextlib.contextmanager
def desktop_lock(owner: str, timeout: float = 900.0, poll: float = 0.5, name: str = "desktop") -> Iterator[None]:
    lock_path, owner_path = _lock_paths(name)
    lock_path.touch(exist_ok=True)
    fh = open(lock_path, "r+b")  # noqa: SIM115 - held for the duration of the context
    deadline = time.monotonic() + timeout
    announced = False
    try:
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{name} lock still held by {_current_owner(name)} after {timeout:.0f}s") from None
                if not announced:
                    print(f"[desktop_lock] waiting for the {name} lock held by {_current_owner(name)} ...", file=sys.stderr)
                    announced = True
                time.sleep(poll)
        owner_path.write_text(f"{owner} (pid {os.getpid()}, since {time.strftime('%H:%M:%S')})", "utf-8")
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                owner_path.write_text("nobody", "utf-8")
            fh.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fh.close()


def _current_owner(name: str = "desktop") -> str:
    try:
        return _lock_paths(name)[1].read_text("utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def main(argv: list[str]) -> int:
    name = "desktop"
    if "--name" in argv:
        i = argv.index("--name")
        name = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    if len(argv) >= 1 and argv[0] == "status":
        print(_current_owner(name))
        return 0
    if len(argv) >= 3 and argv[0] == "run" and "--" in argv:
        owner = argv[1]
        command = argv[argv.index("--") + 1 :]
        with desktop_lock(owner, name=name):
            return subprocess.call(command)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
