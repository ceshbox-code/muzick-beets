#!/usr/bin/env python3
"""Cross-container advisory lock for all commands sharing the beets database."""
import fcntl
import os
import subprocess
import sys

LOCK = "/config/.muzick.lock"

def main():
    if len(sys.argv) < 2:
        print("usage: lockrun.py COMMAND [ARG...]", file=sys.stderr)
        return 64
    os.makedirs("/config", exist_ok=True)
    with open(LOCK, "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Другая задача уже работает с базой beets; дождитесь её завершения.", file=sys.stderr)
            return 75
        try:
            return subprocess.call(sys.argv[1:])
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

if __name__ == "__main__":
    raise SystemExit(main())
