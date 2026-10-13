import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tools import install_reader_bridge as install

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.reader=root/'reader';self.reader.mkdir()
        self.ledger=root/'private';self.ledger.mkdir(mode=0o700)
        subprocess.run([sys.executable,'-m','review','--db',str(self.ledger/'ledger.sqlite'),'demo'],stdout=subprocess.DEVNULL,check=True)
        self.base=b'synthetic original app';(self.reader/'app.py').write_bytes(self.base)
        (self.reader/'Dockerfile').write_text('FROM python:3.12-slim\nWORKDIR /app\nCOPY app.py /app/app.py\nUSER 65534:65534\nCMD ["python", "app.py"]\n')
        (self.reader/'compose.yaml').write_text('synthetic base compose\n')
        (self.reader/'.env').write_text('SYNTHETIC_SECRET=private\n')
        self.args=argparse.Namespace(reader_dir=str(self.reader),ledger_dir=str(self.ledger),service='reader',uid=os.getuid(),gid=os.getgid(),expected_image='sha256:'+'2'*64)
        self.cfg={'services':{'reader':{'build':{'context':str(self.reader),'dockerfile':'Dockerfile'},'ports':[{'host_ip':'127.0.0.1','published':'8787','target':8787,'protocol':'tcp'}],'security_opt':['no-new-privileges:true']}}}
        self.current={'image':self.args.expected_image,'user':'65534:65534','mounts':[],'privileged':False,'userns':'','groups':None,'caps':None,'pid':'','security':['no-new-privileges:true']}
        self.calls=[];self.fail_build=False;self.fail_up=False
        self.sha=patch.object(install,'BASE_SHA',hashlib.sha256(self.base).hexdigest());self.sha.start();self.addCleanup(self.sha.stop)
        self.health=patch.object(install,'health');self.health.start();self.addCleanup(self.health.stop)
        self.runner=patch.object(install,'run',side_effect=self.fake_run);self.runner.start();self.addCleanup(self.runner.stop)

    def fake_run(self,args):
        self.calls.append(args)
        if '--format' in args and 'config' in args:return json.dumps(self.cfg)
        if 'ps' in args:return 'a'*64
        if 'inspect' in args:return json.dumps(self.current)
        if 'info' in args:return '["name=seccomp,profile=builtin"]'
        if 'build' in args and self.fail_build:raise RuntimeError('synthetic failure')
        if 'up' in args and self.fail_up:
            self.fail_up=False;raise RuntimeError('synthetic failure')
        return ''

    def preflight(self):
        return install.preflight(self.reader,self.ledger,'reader',self.args.uid,self.args.gid,self.args.expected_image)

    def test_failed_preflight_has_no_files_or_docker_mutations(self):
        baseline={p.name:p.read_bytes() for p in self.reader.iterdir()}
        for field,value in [('volumes',['unexpected']),('privileged',True),('user','root'),('cap_add',['SYS_ADMIN'])]:
            self.cfg['services']['reader'][field]=value
            with self.assertRaises(ValueError):install.install(self.args)
            del self.cfg['services']['reader'][field]
            self.assertEqual(baseline,{p.name:p.read_bytes() for p in self.reader.iterdir()})
            self.assertFalse((self.reader.parent/'.reader-bridge-install').exists())
        self.assertFalse(any('build' in c or 'up' in c or 'tag' in c for c in self.calls))

    def test_wrong_hash_or_port_is_nonmutating(self):
        (self.reader/'app.py').write_bytes(b'wrong synthetic source')
        with self.assertRaises(ValueError): install.install(self.args)
        self.assertFalse((self.reader.parent/'.reader-bridge-install').exists())
        (self.reader/'app.py').write_bytes(self.base)
        self.cfg['services']['reader']['ports'][0]['host_ip']='0.0.0.0'
        with self.assertRaises(ValueError): install.install(self.args)
        self.assertFalse((self.reader.parent/'.reader-bridge-install').exists())

    def test_build_failure_does_not_recreate_or_change_originals(self):
        self.fail_build=True
        with self.assertRaises(RuntimeError):install.install(self.args)
        self.assertFalse((self.reader/'compose.override.yaml').exists())
        self.assertFalse(any('up' in c for c in self.calls))
        state=self.reader.parent/'.reader-bridge-install'
        self.assertFalse((state/'build'/'.env').exists());self.assertFalse((state/'backup'/'.env').exists())
        self.assertEqual((self.reader/'app.py').read_bytes(),self.base)
        with self.assertRaises(ValueError):install.install(self.args)

    def test_install_repeat_guard_and_rollback(self):
        state=install.install(self.args)
        self.assertTrue((state/'INSTALLED').exists())
        override=json.loads((self.reader/'compose.override.yaml').read_text())['services']['reader']
        self.assertEqual(override['user'],str(self.args.uid)+':'+str(self.args.gid))
        self.assertTrue(override['volumes'][0]['read_only'])
        self.assertNotIn('environment',override);self.assertNotIn('ports',override)
        self.assertTrue(override['image'].startswith('review-reader-bridge:'))
        context=Path(override['build']['context'])
        self.assertEqual(sorted(p.name for p in context.iterdir()),['Dockerfile','app.py','review'])
        self.assertIn('COPY review/__init__.py review/ledger.py review/readonly.py review/write_lock.py review/verified_sync.py /app/review/',(context/'Dockerfile').read_text())
        with self.assertRaises(ValueError):install.install(self.args)
        install.rollback(state)
        self.assertFalse((self.reader/'compose.override.yaml').exists())
        self.assertEqual((self.reader/'app.py').read_bytes(),self.base)
        up=[c for c in self.calls if 'up' in c]
        self.assertTrue(all('--no-deps' in c and '--no-build' in c and c[-1]=='reader' for c in up))
        self.assertEqual(json.loads((state/'rollback.json').read_text())['services']['reader']['image'],'review-reader-rollback:'+'2'*12)

    def test_recreate_failure_rolls_back(self):
        self.fail_up=True
        with self.assertRaises(RuntimeError):install.install(self.args)
        state=self.reader.parent/'.reader-bridge-install'
        self.assertTrue((state/'ROLLED_BACK').exists())
        self.assertFalse((self.reader/'compose.override.yaml').exists())

    def test_health_failure_rolls_back(self):
        with patch.object(install, 'health', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError): install.install(self.args)
        self.assertTrue((self.reader.parent/'.reader-bridge-install'/'ROLLED_BACK').exists())

    def test_preserve_existing_env_and_healthcheck(self):
        original = ('FROM python:3.12-slim\r\nWORKDIR /app\r\n'
                    'ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\r\n'
                    'COPY app.py /app/app.py\r\nUSER 65534:65534\r\n'
                    'EXPOSE 8787\r\nHEALTHCHECK --interval=30s CMD python -c "print(1)"\r\n'
                    'CMD ["python", "app.py"]\r\n').encode()
        (self.reader/'Dockerfile').write_bytes(original)
        state=install.install(self.args)
        staged=(state/'build'/'Dockerfile').read_bytes()
        self.assertEqual(staged, original+b'\nCOPY review/__init__.py review/ledger.py review/readonly.py review/write_lock.py review/verified_sync.py /app/review/\n')
        self.assertEqual((self.reader/'Dockerfile').read_bytes(), original)
        self.assertEqual((state/'backup'/'Dockerfile').read_bytes(),original)

    def test_continuations_are_arguments_not_new_instructions(self):
        slash=chr(92)
        source=('FROM python:3.12-slim\nWORKDIR /app\n'
                'ENV PYTHONDONTWRITEBYTECODE=1 '+slash+'\n'
                '# synthetic comment\n PYTHONUNBUFFERED=1\n'
                'COPY app.py /app/app.py\nUSER 65534:65534\nEXPOSE 8787\n'
                'HEALTHCHECK --interval=30s '+slash+'\n CMD python -c "print(1)"\n'
                'CMD ["python", "app.py"]\n')
        parsed=install.dockerfile_instructions(source)
        self.assertEqual([x.split()[0] for x in parsed],['FROM','WORKDIR','ENV','COPY','USER','EXPOSE','HEALTHCHECK','CMD'])
        (self.reader/'Dockerfile').write_text(source)
        self.preflight()
        self.assertFalse((self.reader.parent/'.reader-bridge-install').exists())

    def test_unapproved_instructions_and_layout_fail_closed(self):
        source=(self.reader/'Dockerfile').read_text()
        for altered in (source+'RUN SYNTHETIC_SECRET\n', source+'ENV SYNTHETIC_SECRET=value\n',
                        source+'HEALTHCHECK NONE\n', '# escape=`\n'+source,
                        '# syntax=unapproved/frontend\n'+source, source+chr(92)):
            with self.assertRaises(ValueError) as error: install.dockerfile_instructions(altered)
            self.assertNotIn('SYNTHETIC_SECRET',str(error.exception))

    def test_changed_base_blocks_rollback_before_override_removal(self):
        state=install.install(self.args)
        (self.reader/'compose.yaml').write_text('changed base')
        with self.assertRaises(ValueError):install.rollback(state)
        self.assertTrue((self.reader/'compose.override.yaml').exists())

    def test_changed_override_blocks_rollback(self):
        state=install.install(self.args)
        (self.reader/'compose.override.yaml').write_text('changed')
        with self.assertRaises(ValueError):install.rollback(state)
        self.assertTrue((self.reader/'compose.override.yaml').exists())


class DockerSudoTests(unittest.TestCase):
    def test_sudo_wraps_only_docker_noninteractively(self):
        from types import SimpleNamespace
        with patch.object(install, 'SUDO_DOCKER', True), patch.object(install.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='ok')) as child:
            self.assertEqual(install.run(['docker','compose','ps']), 'ok')
            self.assertEqual(child.call_args.args[0], ['sudo','-n','docker','compose','ps'])
            install.run(['other','command'])
            self.assertEqual(child.call_args.args[0], ['other','command'])

    def test_default_docker_and_sudo_failure_are_safe(self):
        from types import SimpleNamespace
        with patch.object(install, 'SUDO_DOCKER', False), patch.object(install.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='ok')) as child:
            install.run(['docker','info'])
            self.assertEqual(child.call_args.args[0], ['docker','info'])
        with patch.object(install, 'SUDO_DOCKER', True), patch.object(install.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='SYNTHETIC_SECRET', stderr='SYNTHETIC_SECRET')):
            with self.assertRaises(RuntimeError) as error: install.run(['docker','info'])
            self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))
