"""Cross-platform subprocess launch helper.

On Windows, CLI tools installed via npm (`claude`, `codex`, etc.) are
`.cmd`/`.bat` shims, not standalone PE executables.
`asyncio.create_subprocess_exec` calls Win32 `CreateProcess` directly, which
cannot launch a script file without going through a command interpreter — it
raises `FileNotFoundError: [WinError 2]` even though `shutil.which` finds the
shim on PATH. Route through `cmd.exe /c` on Windows so shim-based CLIs
actually launch; POSIX needs no such wrapping.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any


async def create_subprocess(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    if sys.platform == "win32":
        args = ("cmd", "/c", *args)
    return await asyncio.create_subprocess_exec(*args, **kwargs)
