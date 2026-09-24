#!/usr/bin/env python3
"""Open inheritable filler descriptors, then exec a Bend client in the same PID."""
import os
import sys

def main() -> int:
    opened = []
    try:
        separator = sys.argv.index("--")
        target = int(sys.argv[sys.argv.index("--fd-through") + 1])
        command = sys.argv[separator + 1:]
        if target < 1024 or not command:
            raise ValueError("target must be >=1024 and an executable is required")
        highest = -1
        while highest < target:
            fd = os.open("/dev/null", os.O_RDONLY)
            opened.append(fd)
            os.set_inheritable(fd, True)
            highest = fd
        os.execv(command[0], command)
    except Exception as exc:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
        os.write(2, ("high_fd_exec: " + repr(exc) + "\n").encode())
        return 89
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
