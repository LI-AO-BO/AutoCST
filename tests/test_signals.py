"""Real Windows kernel events; no CST, runner task, or model is launched."""

from pathlib import Path
import os
import subprocess
import sys
import time
import unittest
import uuid

from autocst.signals import Event, wait


@unittest.skipUnless(sys.platform == "win32", "Windows kernel event contract")
class SignalTests(unittest.TestCase):
    def name(self):
        return f"Local\\AutoCST-test-{uuid.uuid4().hex}"

    def test_child_process_signals_manual_event_without_polling(self):
        name = self.name()
        child_code = (
            "import os,sys; from autocst.signals import Event; "
            "event=Event(sys.argv[1],manual=True); event.set(); "
            "print(os.getpid(),flush=True); event.close()"
        )
        # Register before launch: the child's signal remains set until reset,
        # even if the child exits before the parent enters the kernel wait.
        with Event(name, manual=True) as event:
            started = time.monotonic()
            with subprocess.Popen(
                [sys.executable, "-c", child_code, name],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ) as child:
                signalled = wait([event], timeout_seconds=10)
                stdout, stderr = child.communicate(timeout=10)
                elapsed = time.monotonic() - started
            self.assertEqual(signalled, 0, stderr)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertNotEqual(int(stdout.strip()), os.getpid())
            self.assertLess(elapsed, 10)
            self.assertEqual(wait([event], timeout_seconds=0), 0)
            event.reset()
            self.assertIsNone(wait([event], timeout_seconds=0))

    def test_zero_deadline_on_unsignalled_event(self):
        with Event(self.name(), manual=True) as event:
            self.assertIsNone(wait([event], timeout_seconds=0))

    def test_event_identity_does_not_wake_another_run(self):
        with Event(self.name(), manual=True) as first, Event(self.name(), manual=True) as second:
            second.set()
            self.assertEqual(wait([first, second], timeout_seconds=0), 1)
            self.assertIsNone(wait([first], timeout_seconds=0))


if __name__ == "__main__":
    unittest.main()
