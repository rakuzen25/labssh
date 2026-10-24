"""Small live console: no terminal framework or background rendering thread."""

import os
import select
import sys


class Console:
    def __enter__(self):
        # Import presentation only when opening the interactive menu.
        from .presentation import MenuRenderer

        self.renderer = MenuRenderer()
        if os.name == "nt":
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
            self.old_mode = old.value
            if not self.kernel.SetConsoleMode(self.handle, old.value | 4):
                os.close(self.console_fd)
                raise OSError("Could not enable terminal rendering")
        else:
            import termios
            import tty

            self.old_mode = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        try:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
        except BaseException:
            # A failing __enter__ is not followed by __exit__ automatically.
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *args):
        try:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        finally:
            if os.name == "nt":
                try:
                    self.kernel.SetConsoleMode(self.handle, self.old_mode)
                finally:
                    os.close(self.console_fd)
            else:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_mode)

    def key(self):
        if os.name == "nt":
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

    def draw(self, state, typed="", message=""):
        import shutil

        width, height = shutil.get_terminal_size((100, 40))
        output = self.renderer.render(
            state, typed, message, width=max(1, width - 1), height=height
        )
        sys.stdout.write(
            "\x1b[H" + "\x1b[K\n".join(output.splitlines()) + "\x1b[K\x1b[J"
        )
        sys.stdout.flush()
