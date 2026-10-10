import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import test_installer as fixture
from tools import install_reader_bridge as installer

spec=importlib.util.spec_from_file_location('bridge_diagnostic', Path(__file__).resolve().parents[1]/'tools'/'diagnose-reader-bridge.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)

class DiagnosticTests(unittest.TestCase):
    setUp = fixture.InstallerTests.setUp
    fake_run = fixture.InstallerTests.fake_run

    def snapshot(self):
        return {str(p.relative_to(self.reader.parent)):(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.reader.parent.rglob('*') if p.is_file()}

    def diagnose(self):
        self.args.sudo_docker=True
        output=io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(installer,'install',side_effect=AssertionError('must not install')), patch.object(installer,'rollback',side_effect=AssertionError('must not rollback')):
            result=diag.diagnose(self.args)
        return result,output.getvalue()

    def test_success_no_writes_or_mutating_commands(self):
        before=self.snapshot();result,output=self.diagnose()
        self.assertEqual(result,0);self.assertIn('RECOVERY_STATE=NO',output)
        self.assertIn('NO_INSTALL',output);self.assertEqual(before,self.snapshot())
        self.assertFalse((self.reader.parent/'.reader-bridge-install').exists())
        self.assertFalse(any('build' in c or 'up' in c or 'tag' in c for c in self.calls))

    def test_validation_fixed_code_no_writes(self):
        (self.reader/'app.py').write_bytes(b'changed source')
        before=self.snapshot();result,output=self.diagnose()
        self.assertEqual(result,1);self.assertIn('E_SOURCE_HASH',output)
        self.assertIn('PREFLIGHT_SOURCE',output);self.assertEqual(before,self.snapshot())

    def test_command_failure_does_not_print_secret_or_traceback(self):
        before=self.snapshot()
        with patch.object(installer,'run',side_effect=RuntimeError('SYNTHETIC_SECRET')):
            result,output=self.diagnose()
        self.assertEqual(result,1);self.assertIn('PREFLIGHT_DOCKER_CONFIG',output)
        self.assertNotIn('SYNTHETIC_SECRET',output);self.assertNotIn('Traceback',output)
        self.assertEqual(before,self.snapshot())

    def test_allowlist_refuses_mutating_preflight_regression(self):
        with patch.object(installer,'preflight',side_effect=lambda *args: installer.run(['docker','compose','up','reader'])):
            result,output=self.diagnose()
        self.assertEqual(result,1);self.assertEqual(self.calls,[])
