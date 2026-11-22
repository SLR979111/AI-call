"""Start the Windows server with only its own log handles and report its PID."""

import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    runtime = root / "runtime"
    args = [sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(int(sys.argv[1]))]
    # Windows PowerShell 5 can pass its caller's pipe handles to Start-Process.
    # An explicit handle list lets headless callers see EOF as soon as start.ps1 exits.
    with (runtime / "server.stdout.log").open("wb") as out, (runtime / "server.stderr.log").open("wb") as err:
        server = subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL, stdout=out,
                                  stderr=err, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
    print(server.pid)


if __name__ == "__main__":
    main()
