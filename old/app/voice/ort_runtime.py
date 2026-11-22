"""Windows ORT bridge: use the vendored onnxruntime before sherpa-onnx loads.

sherpa-onnx 2.0.1 requires ONNX Runtime API 28. On Windows, a system-wide
C:\\Windows\\System32\\onnxruntime.dll (1.17) wins the default DLL search, which
makes sherpa crash or report an API mismatch. Loading a vendored 1.28 DLL by
absolute path first pins the correct ABI for this process.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def preload_onnxruntime() -> str | None:
    if sys.platform != "win32":
        return None
    root = Path(__file__).resolve().parents[2]
    candidates: list[Path] = []
    if os.getenv("ORT_DLL_DIR"):
        candidates.append(Path(os.environ["ORT_DLL_DIR"]) / "onnxruntime.dll")
    candidates.extend([
        root / ".web311" / "onnxruntime" / "capi" / "onnxruntime.dll",
        root / ".vendor311" / "onnxruntime" / "capi" / "onnxruntime.dll",
        root / ".venv311" / "Scripts" / "onnxruntime.dll",
    ])
    for dll in candidates:
        if not dll.is_file():
            continue
        try:
            ctypes.WinDLL(str(dll))
            return str(dll)
        except OSError:
            continue
    return None
