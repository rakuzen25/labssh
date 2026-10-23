"""Terminal restoration tests without opening a real console."""

import ctypes
import io
import sys
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import labssh.console


class ConsoleTests(unittest.TestCase):
    @contextmanager
    def terminal(self, platform, output):
        stack = ExitStack()
        with stack:
            yield self.configure_terminal(stack, platform, output)

    def configure_terminal(self, stack, platform, output):
        stack.enter_context(patch.object(labssh.console.os, "name", platform))
        stack.enter_context(patch.object(sys, "stdout", output))
        if platform == "nt":
            kernel = Mock()

            def get_mode(handle, mode):
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
                    {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: 123)},
                )
            )
            stack.enter_context(
                patch.object(labssh.console.os, "open", return_value=42)
            )
            close = stack.enter_context(patch.object(labssh.console.os, "close"))
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

    def test_exit_restores_native_mode_even_if_output_fails(self):
        for platform in ("nt", "posix"):
            output = Mock()
            with (
                self.subTest(platform=platform),
                self.terminal(platform, output) as (native, resource),
            ):
                terminal = labssh.console.Console().__enter__()
                output.flush.side_effect = OSError("output failed")
                with self.assertRaisesRegex(OSError, "output failed"):
                    terminal.__exit__(None, None, None)
                if platform == "nt":
                    self.assertEqual(native.SetConsoleMode.call_args, call(123, 7))
                    resource.assert_called_once_with(42)
                else:
                    native.tcsetattr.assert_called_once_with(0, 1, [7])

    def test_entry_output_failure_restores_native_mode_and_cursor(self):
        for platform in ("nt", "posix"):
            output = Mock(wraps=io.StringIO())
            output.flush.side_effect = OSError("output failed")
            with (
                self.subTest(platform=platform),
                self.terminal(platform, output) as (native, resource),
            ):
                with self.assertRaisesRegex(OSError, "output failed"):
                    labssh.console.Console().__enter__()
                self.assertIn(call("\x1b[?25h\x1b[?1049l"), output.write.call_args_list)
                if platform == "nt":
                    self.assertEqual(native.SetConsoleMode.call_args, call(123, 7))
                    resource.assert_called_once_with(42)
                else:
                    native.tcsetattr.assert_called_once_with(0, 1, [7])


if __name__ == "__main__":
    unittest.main()
