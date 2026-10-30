import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/run_bounded_command.py'


class BoundedCommandTest(unittest.TestCase):
    def test_timeout_stops_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)/'timeout'
            result = subprocess.run([sys.executable, str(SCRIPT), '--out', str(out), '--timeout-s', '0.3',
                                     '--', sys.executable, '-c', 'import time; time.sleep(30)'], timeout=3,
                                    capture_output=True)
            self.assertEqual(result.returncode, 124)
            state = json.loads((out/'supervisor.json').read_text())
            self.assertEqual(state['status'], 'timeout')
            with self.assertRaises(ProcessLookupError):
                os.kill(state['pid'], 0)

    def test_interrupt_stops_process_group_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)/'cancel'
            process = subprocess.Popen([sys.executable, str(SCRIPT), '--out', str(out), '--timeout-s', '30',
                                       '--', sys.executable, '-u', '-c',
                                       'import time; print("saved result", flush=True); time.sleep(30)'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+3
                while not (out/'command.log').exists() or not (out/'command.log').read_text():
                    if time.monotonic() >= deadline:
                        self.fail('child did not start')
                    time.sleep(.02)
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=3)
                self.assertEqual(process.returncode, 130)
                self.assertEqual((out/'command.log').read_text().strip(), 'saved result')
                state = json.loads((out/'supervisor.json').read_text())
                self.assertEqual(state['status'], 'cancelled')
                with self.assertRaises(ProcessLookupError):
                    os.kill(state['pid'], 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
