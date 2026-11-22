"""Start the archived service from its original working directory."""
import os
import sys
from pathlib import Path

import uvicorn


if __name__ == "__main__":
    archive = Path(__file__).resolve().parents[1] / "old"
    os.chdir(archive)
    sys.path.insert(0, str(archive))
    uvicorn.run("app.main:app", host="127.0.0.1", port=8081,
                env_file=".env" if Path(".env").is_file() else None)
