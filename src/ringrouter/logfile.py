"""Keep the launchd log files from growing forever.

launchd appends a job's stdout and stderr to one file and never rotates it. The listener (at
start-up, then daily) and each watchdog run cut their own log down to its most recent part, in
place. launchd opens the file in append mode, so whatever is written next lands at the new end.
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

LOGS_DIR = Path.home() / "Library" / "Logs"


def trim_own_log(fd: int = 1, max_bytes: int = 5_000_000, keep_bytes: int = 1_000_000,
                 only_under: Path | None = LOGS_DIR) -> bool:
    """If `fd` (stdout by default) is a log file over `max_bytes`, keep its last `keep_bytes`.

    Does nothing unless the file is a regular file opened for append whose name ends in .log
    under `only_under`: a terminal, a pipe or a file someone redirected to by hand is left alone.
    Returns True when the file was trimmed. Never raises.
    """
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= max_bytes:
            return False
        if not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND:
            return False            # a writer at a fixed offset would leave a hole after truncation
        path = Path(os.path.realpath(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0].decode()))
        if path.suffix != ".log" or (only_under and Path(os.path.realpath(only_under)) not in path.parents):
            return False
        with open(path, "rb") as f:
            f.seek(st.st_size - keep_bytes)
            tail = f.read()
        tail = tail[tail.find(b"\n") + 1:]                       # start at a whole line
        note = f"--- log trimmed: kept the last {len(tail):,} of {st.st_size:,} bytes ---\n".encode()
        os.truncate(path, 0)
        out = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(out, note + tail)
        finally:
            os.close(out)
        return True
    except Exception:       # AttributeError where F_GETPATH doesn't exist; OSError otherwise
        return False
