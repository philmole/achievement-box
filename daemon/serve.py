"""Supervisor: keep the Achievement Box web app alive.

Runs webapp.py and restarts it if it ever exits abnormally (native
faults included -- see webapp-crash.log for the traceback). This is how
the box should run unattended, on the PC today and the Pi later.

  .venv\\Scripts\\python daemon\\serve.py [webapp args...]
"""

import subprocess
import sys
import time
from pathlib import Path

WEBAPP = Path(__file__).parent / "webapp.py"


def main():
    args = sys.argv[1:] or ["--port", "auto"]
    backoff = 2
    while True:
        print(f"[serve] starting webapp {' '.join(args)}", flush=True)
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, str(WEBAPP), *args])
        ran = time.monotonic() - t0
        if proc.returncode == 0:
            print("[serve] webapp exited cleanly -- done", flush=True)
            return
        # healthy uptime resets the backoff; crash loops slow down
        backoff = 2 if ran > 60 else min(backoff * 2, 60)
        print(f"[serve] webapp died (exit {proc.returncode}) after "
              f"{ran:.0f}s -- restarting in {backoff}s "
              f"(see webapp-crash.log)", flush=True)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
