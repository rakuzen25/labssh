"""Terminal restoration tests without opening a real console."""

import ctypes
import io
import os
import sys
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call, patch

from test_presentation import fixture

import labssh.console

TerminalMocks = tuple[Mock | SimpleNamespace, Mock | SimpleNamespace]


class Output(io.StringIO):
    """Real Rich output stream with a one-shot write failure."""

    fail = False

    def write(self, text: str) -> int:
        if self.fail:
            self.fail = False
            raise OSError("output failed")
        return super().write(text)


class ConsoleTests(unittest.TestCase):
    @contextmanager
    def terminal(self, platform: str, output: Output) -> Generator[TerminalMocks]:
        stack = ExitStack()
        with stack:
            yield self.configure_terminal(stack, platform, output)

    def configure_terminal(
        self, stack: ExitStack, platform: str, output: Output
    ) -> TerminalMocks:
        stack.enter_context(
            patch.object(sys, "platform", "win32" if platform == "nt" else "linux")
        )
        stack.enter_context(patch.object(sys, "stdout", output))
        stack.enter_context(patch.dict(os.environ, {"TERM": "xterm"}))
        if platform == "nt":
            kernel = Mock()

            def get_mode(handle: int, mode: Any) -> int:
                mode._obj.value = 7
                return 1

            kernel.GetConsoleMode.side_effect = get_mode
            kernel.SetConsoleMode.return_value = 1
            stack.enter_context(
                patch.object(ctypes, "WinDLL", return_value=kernel, create=True)
            )
            stack.enter_context(
                patch.dict(
                    sys.modules,
                    {"msvcrt": SimpleNamespace(get_osfhandle=Mock(return_value=123))},
                )
            )
            stack.enter_context(patch.object(os, "open", return_value=42))
            close = stack.enter_context(patch.object(os, "close"))
            return kernel, close
        termios = SimpleNamespace(
            tcgetattr=Mock(return_value=[7]), tcsetattr=Mock(), TCSADRAIN=1
        )
        tty = SimpleNamespace(setcbreak=Mock())
        stack.enter_context(patch.dict(sys.modules, {"termios": termios, "tty": tty}))
        stack.enter_context(
            patch.object(sys, "stdin", SimpleNamespace(fileno=lambda: 0))
        )
        return termios, tty

    def test_exit_restores_native_mode_even_if_output_fails(self) -> None:
        for platform in ("nt", "posix"):
            output = Output()
            with (
                self.subTest(platform=platform),
                self.terminal(platform, output) as (native, resource),
            ):
                terminal = labssh.console.Console().__enter__()
                output.fail = True
                with self.assertRaisesRegex(OSError, "output failed"):
                    terminal.__exit__(None, None, None)
                if platform == "nt":
                    self.assertEqual(native.SetConsoleMode.call_args, call(123, 7))
                    resource.assert_called_once_with(42)
                else:
                    native.tcsetattr.assert_called_once_with(0, 1, [7])
                self.assertIn("\x1b[?25h", output.getvalue())
                self.assertIn("\x1b[?1049l", output.getvalue())

    def test_entry_output_failure_restores_native_mode_and_cursor(self) -> None:
        for platform in ("nt", "posix"):
            output = Output()
            output.fail = True
            with (
                self.subTest(platform=platform),
                self.terminal(platform, output) as (native, resource),
            ):
                with self.assertRaisesRegex(OSError, "output failed"):
                    labssh.console.Console().__enter__()
                self.assertIn("\x1b[?25h", output.getvalue())
                self.assertIn("\x1b[?1049l", output.getvalue())
                if platform == "nt":
                    self.assertEqual(native.SetConsoleMode.call_args, call(123, 7))
                    resource.assert_called_once_with(42)
                else:
                    native.tcsetattr.assert_called_once_with(0, 1, [7])

    def test_live_updates_resize_and_no_color_keep_terminal_controls(self) -> None:
        output = Output()
        with (
            self.terminal("nt", output),
            patch.dict(os.environ, {"NO_COLOR": "1"}),
            patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 24))),
        ):
            with labssh.console.Console() as terminal:
                state = fixture()
                terminal.draw(state, "2", "Auto-connect in 3s")
                first_frame = output.getvalue()
                with patch(
                    "shutil.get_terminal_size", return_value=os.terminal_size((32, 7))
                ):
                    terminal.draw(state, "3")
                self.assertFalse(terminal.live.auto_refresh)
            self.assertIn("SSH 1150 ms", first_frame)
            self.assertIn("Auto-connect in 3s", first_frame)
            self.assertIn("\x1b[?1049h", first_frame)
            self.assertIn("\x1b[?1049l", output.getvalue())
            self.assertNotRegex(output.getvalue(), r"\x1b\[[0-9;]*m")

    def test_render_failure_restores_display_and_native_mode(self) -> None:
        output = Output()
        with self.terminal("nt", output) as (kernel, close):
            with (
                self.assertRaisesRegex(OSError, "output failed"),
                labssh.console.Console() as terminal,
            ):
                output.fail = True
                terminal.draw(fixture())
            self.assertIn("\x1b[?25h", output.getvalue())
            self.assertIn("\x1b[?1049l", output.getvalue())
            self.assertEqual(kernel.SetConsoleMode.call_args, call(123, 7))
            close.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
