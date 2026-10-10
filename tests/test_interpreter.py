"""Child commands must work when PATH provides no 'python' alias."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class InterpreterTests(unittest.TestCase):
    def test_http_suite_without_python_on_path(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as empty_path:
            self.assertIsNone(shutil.which('python', path=empty_path))
            result = subprocess.run(
                [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                 '-p', 'test_review_http.py', '-v'],
                cwd=repo, env=dict(os.environ, PATH=empty_path),
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Ran 5 tests', result.stderr)


if __name__ == '__main__':
    unittest.main()
