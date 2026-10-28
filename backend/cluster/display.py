"""Start an instance's X server without locks left by a container restart.

Docker preserves /tmp across stop/start. An old X lock can name a PID now
belonging to a different display, so checking that PID alone is insufficient.
Only remove the display's files when neither of its Unix sockets is listening.
Supervisor is the sole launcher for each display.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import socket
import sys


def clear_stale_display(display: str, tmp: Path = Path("/tmp")) -> bool:
    if not re.fullmatch(r":[0-9]+", display):
        raise ValueError("Expected an X display number, such as :100")
    number = display[1:]
    endpoint = tmp / ".X11-unix" / f"X{number}"
    addresses = [str(endpoint)]
    if sys.platform.startswith("linux"):
        addresses.append("\0" + str(endpoint))
    for address in addresses:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(address)
                return False  # A live X server owns this display; leave it alone.
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise  # Permission errors/timeouts do not prove a stale socket.
    (tmp / f".X{number}-lock").unlink(missing_ok=True)
    endpoint.unlink(missing_ok=True)
    return True


def main() -> None:
    clear_stale_display(sys.argv[1])
    os.execv("/usr/bin/Xvfb", ["Xvfb", *sys.argv[1:]])


if __name__ == "__main__":
    main()
