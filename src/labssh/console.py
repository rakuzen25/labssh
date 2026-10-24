"""Small live console: no terminal framework or background rendering thread."""

from __future__ import annotations

import os
import select
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from .cli import Selection


class Console:
    def __enter__(self) -> Self:
        # Import presentation only when opening the interactive menu.
        from rich.live import Live

        from .presentation import make_console

        self.live = Live(
            console=make_console(),
            screen=True,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
            vertical_overflow="crop",
        )
        if sys.platform == "win32":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            self.kernel.GetStdHandle.restype = wintypes.HANDLE
            self.kernel.GetConsoleMode.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            # Python launchers may reopen CRT descriptors without replacing the
            # process-wide Win32 standard handles. Use the actual stdout fd.
            # CONOUT$ reopened by a shell redirection can be write-only, while
            # GetConsoleMode requires read access. Open the same console R/W.
            self.console_fd = os.open("CONOUT$", os.O_RDWR)
            self.handle = msvcrt.get_osfhandle(self.console_fd)
            old = wintypes.DWORD()
            if not self.kernel.GetConsoleMode(self.handle, ctypes.byref(old)):
                os.close(self.console_fd)
                raise OSError("Live selection requires a Windows console")
            self.old_output_mode = old.value
            if not self.kernel.SetConsoleMode(self.handle, old.value | 4):
                os.close(self.console_fd)
                raise OSError("Could not enable terminal rendering")
        else:
            import termios
            import tty

            self.old_mode = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        try:
            self.live.start()
        except BaseException:
            # A failing __enter__ is not followed by __exit__ automatically.
            # Preserve the startup error if Rich was only partly entered.
            with suppress(Exception):
                self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *args: object) -> None:
        try:
            self.live.stop()
        except BaseException:
            # Live may fail before it records the alternate-screen change, or
            # while restoring it. A fresh console avoids its failed buffer.
            from rich.control import Control

            from .presentation import make_console

            make_console().control(Control.show_cursor(True), Control.alt_screen(False))
            raise
        finally:
            if sys.platform == "win32":
                try:
                    self.kernel.SetConsoleMode(self.handle, self.old_output_mode)
                finally:
                    os.close(self.console_fd)
            else:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_mode)

    def key(self) -> str | None:
        if sys.platform == "win32":
            import msvcrt

            if not msvcrt.kbhit():
                return None
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                msvcrt.getwch()
                # Preserve input detection even for keys without menu actions.
                return "\x00"
            return key
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None

    def draw(self, state: Selection, typed: str = "", message: str = "") -> None:
        import shutil

        from .presentation import menu

        width, height = shutil.get_terminal_size((100, 40))
        self.live.update(
            menu(state, typed, message, width=max(1, width - 1), height=height),
            refresh=True,
        )
