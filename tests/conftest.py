"""Minimal async test support without pytest-asyncio.

This keeps the repo runnable in constrained student environments where only
pytest is installed. If pytest-asyncio is available, its plugin is compatible;
this hook simply handles coroutine test functions directly.
"""

from __future__ import annotations

import asyncio
import inspect


def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        names = inspect.signature(pyfuncitem.obj).parameters
        asyncio.run(pyfuncitem.obj(**{k: v for k, v in pyfuncitem.funcargs.items() if k in names}))
        return True
    return None
