"""Synthetic deployment fixtures; never use Docker, sudo or real credentials."""
import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import deploy_chat_sync as deploy

NGINX='''# preserved comment { }
server {
 server_name gate-reader.gatelens.site;
 location / { proxy_pass http://127.0.0.1:8787; add_header X-Test "a;}#b"; }
 listen 443 ssl;
 ssl_certificate /synthetic/fullchain.pem;
 ssl_certificate_key /synthetic/privkey.pem;
 include /etc/letsencrypt/options-ssl-nginx.conf;
}
server { listen 80; server_name gate-reader.gatelens.site; return 301 https://$host$request_uri; }
'''

class NginxTests(unittest.TestCase):
    def test_preserves_original_with_quotes_comments_and_other_servers(self):
        result=deploy.nginx_route(NGINX)
        start=result.index('\n    location = /review-sync')
        end=result.index('\n}',start)
        self.assertEqual(result[:start]+result[end+1:],NGINX)
        self.assertEqual(result.count('location = /review-sync'),1)
        self.assertIn('proxy_next_upstream off;',result)
        self.assertIn('proxy_read_timeout 1800s;',result)

    def test_ambiguous_or_unsupported_configuration_stops(self):
        bad=[NGINX+NGINX,NGINX.replace('443','444'),NGINX.replace('listen 443 ssl;','if ($x) { return 403; } listen 443 ssl;'),
             NGINX.replace('listen 443 ssl;','include /synthetic/unknown.conf; listen 443 ssl;'),
             NGINX.replace('location /','location = /review-sync'),NGINX[:-2],NGINX+' stray',
             'http { '+NGINX+' }']
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError): deploy.nginx_route(text)

    def test_cleartext_mixed_listeners_and_aliases_are_rejected(self):
        for value in ('listen 443;', 'listen 443 ssl; listen 80;', 'listen 443 ssl quic;'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):deploy.nginx_route(NGINX.replace('listen 443 ssl;',value))
        with self.assertRaises(ValueError):
            deploy.nginx_route(NGINX.replace('server_name gate-reader.gatelens.site;', 'server_name gate-reader.gatelens.site other.example;',1))

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.reader=root/'reader';self.reader.mkdir()
        self.data=root/'data';self.data.mkdir(mode=0o700)
        (self.data/'ledger.sqlite').write_bytes(b'synthetic ledger');(self.data/'ledger.sqlite').chmod(0o600)
        self.nginx=root/'nginx.conf';self.nginx.write_text(NGINX)
        context=root/'.reader-bridge-install'/'build';context.mkdir(parents=True)
        backup=context.parent/'backup';backup.mkdir()
        (backup/'Dockerfile').write_text('FROM python:3.12-slim\nWORKDIR /app\nCOPY app.py /app/app.py\nUSER 65534:65534\nCMD ["python", "app.py"]\n')
        (self.reader/'compose.yaml').write_text('synthetic base')
        self.override=self.reader/'compose.override.yaml'
        self.old={'services':{'reader':{'image':'synthetic-old','user':'1000:1001','build':{'context':str(context),'dockerfile':'Dockerfile'},
            'volumes':[{'type':'bind','source':str(self.data),'target':'/review-data','read_only':True}]}}}
        self.override.write_text(json.dumps(self.old))
        self.original=self.override.read_bytes()
        self.cfg=copy.deepcopy(self.old);self.cfg['services']['reader']['ports']=[{'host_ip':'127.0.0.1','published':'8787','target':8787}]
        self.current={'image':'sha256:'+'3'*64,'user':'1000:1001','mounts':[{'Type':'bind','Source':str(self.data),'Destination':'/review-data','RW':False}],
            'privileged':False,'caps':None,'pid':'','userns':''}
        self.a=argparse.Namespace(reader=self.reader,data=self.data,state=root/'state',nginx=self.nginx,checkout=root/'checkout',commit='a'*40,
            expected_image=self.current['image'],uid=1000,gid=1001)
        self.unit=root/'systemd'/'gate-review-sync.service';self.unit.parent.mkdir()
        self.env=root/'credentials.env';self.calls=[];self.fail_build=False;self.fail_nginx=False
        patches=[patch.object(deploy,'sudo',side_effect=self.sudo),patch.object(deploy,'check_source'),patch.object(deploy,'private'),
                 patch.object(deploy.os,'getuid',return_value=1000),patch.object(deploy,'UNIT',self.unit),patch.object(deploy,'ENV',self.env),
                 patch.object(deploy,'worker_health'),patch.object(deploy,'reader_smoke'),patch('tools.install_reader_bridge.health')]
        for item in patches:item.start();self.addCleanup(item.stop)

    def sudo(self,*args,payload=None):
        args=tuple(map(str,args));self.calls.append((args,payload))
        if args[0]=='cat':return Path(args[1]).read_bytes()
        if args[0]=='stat':return b'0:600'
        if args[0]=='docker':
            if args[1:3]==('image','inspect'):return ('sha256:'+'5'*64).encode()
            if 'config' in args:return json.dumps(self.cfg).encode()
            if 'ps' in args:return b'b'*64
            if 'inspect' in args:
                return json.dumps(['GATE_API_KEY=SYNTHETIC_KEY_PRIVATE','GATE_API_SECRET=SYNTHETIC_SECRET_PRIVATE','READER_TOKEN='+'r'*64]).encode() if '.Config.Env' in args[-2] else json.dumps(self.current).encode()
            if 'build' in args and self.fail_build:raise ValueError('synthetic build failure')
        if args[0]=='install':
            Path(args[-1]).write_bytes(payload if args[-2]=='/dev/stdin' else Path(args[-2]).read_bytes())
        if args[0]=='mktemp':
            fd,name=tempfile.mkstemp(dir=Path(args[1]).parent);os.close(fd);return name.encode()
        if args[0]=='mv':os.replace(args[-2],args[-1])
        if args[0]=='rm':
            for path in args[2:]:Path(path).unlink(missing_ok=True)
        if args[0]=='nginx' and self.fail_nginx:
            self.fail_nginx=False;raise ValueError('synthetic nginx failure')
        return b''

    def test_preflight_failure_is_nonmutating(self):
        self.nginx.write_text(NGINX.replace('listen 443 ssl;','return 403; listen 443 ssl;'))
        with self.assertRaises(ValueError): deploy.prepare(self.a)
        self.assertFalse(self.a.state.exists());self.assertFalse(self.env.exists())
        self.assertEqual(self.override.read_bytes(),self.original)
        self.assertFalse(any('up' in c for c,_ in self.calls))

    def test_unsafe_build_mount_or_privilege_stops(self):
        for field,value in [('privileged',True),('caps',['SYS_ADMIN']),('image','wrong')]:
            current=dict(self.current,**{field:value})
            with self.assertRaises(ValueError):deploy.validate_reader(self.cfg,current,self.reader,self.data,self.current['image'],1000,1001)
        self.cfg['services']['reader']['build']['args']={'SYNTHETIC':'private'}
        with self.assertRaises(ValueError): deploy.prepare(self.a)
        self.assertFalse(self.a.state.exists())

    def test_build_failure_does_not_recreate_or_configure_credentials(self):
        self.fail_build=True
        with self.assertRaises(ValueError): deploy.prepare(self.a)
        self.assertFalse(self.env.exists());self.assertFalse(self.unit.exists())
        self.assertEqual(self.override.read_bytes(),self.original);self.assertEqual(self.nginx.read_text(),NGINX)
        self.assertFalse(any('up' in c for c,_ in self.calls))

    def test_prepare_keeps_secrets_out_of_context_and_live_configs(self):
        deploy.prepare(self.a)
        self.assertEqual(self.override.read_bytes(),self.original);self.assertEqual(self.nginx.read_text(),NGINX)
        secret=self.env.read_text()
        self.assertIn('READER_TOKEN='+'r'*64,secret);self.assertIn('REVIEW_SYNC_TOKEN=',secret)
        self.assertNotIn('GATE_API',secret)
        for path in (self.a.state/'build').rglob('*'):
            if path.is_file():
                self.assertNotIn(b'SYNTHETIC_KEY_PRIVATE',path.read_bytes());self.assertNotIn(b'r'*64,path.read_bytes())
        self.assertEqual(sorted(p.name for p in (self.a.state/'build').iterdir()),['Dockerfile','app.py','review'])
        self.assertFalse(any('up' in c for c,_ in self.calls))
        with self.assertRaises(ValueError): deploy.prepare(self.a)

    def test_activate_and_rollback_are_reader_only_and_preserve_ledger(self):
        deploy.prepare(self.a);deploy.activate(self.a)
        self.assertTrue((self.a.state/'ACTIVE').exists());self.assertIn('location = /review-sync',self.nginx.read_text())
        self.assertIn('BindPaths='+str(self.data)+':/review-data',self.unit.read_text())
        self.assertIn('Restart=no',self.unit.read_text())
        deploy.rollback(self.a)
        self.assertEqual(self.nginx.read_text(),NGINX);self.assertEqual(self.override.read_bytes(),self.original)
        self.assertEqual((self.data/'ledger.sqlite').read_bytes(),b'synthetic ledger')
        ups=[c for c,_ in self.calls if 'up' in c]
        self.assertEqual(len(ups),2)
        for c in ups:self.assertEqual(c[-1],'reader');self.assertIn('--no-deps',c);self.assertIn('--no-build',c)
        self.assertFalse(any('down' in c or 'prune' in c for c,_ in self.calls))

    def test_activation_failure_rolls_back(self):
        deploy.prepare(self.a);self.fail_nginx=True
        with self.assertRaisesRegex(ValueError,'rollback completed'):deploy.activate(self.a)
        self.assertEqual(self.nginx.read_text(),NGINX);self.assertEqual(self.override.read_bytes(),self.original)
        self.assertTrue((self.a.state/'ROLLED_BACK').exists());self.assertFalse((self.a.state/'ACTIVE').exists())

    def test_drift_is_not_overwritten(self):
        deploy.prepare(self.a);self.nginx.write_text(NGINX+'# changed\n')
        calls=len(self.calls)
        with self.assertRaises(ValueError):deploy.activate(self.a)
        self.assertFalse(any('up' in c for c,_ in self.calls[calls:]))
        with self.assertRaises(ValueError):deploy.rollback(self.a)
        self.assertTrue(self.nginx.read_text().endswith('# changed\n'))

    def test_activation_rechecks_container_and_unit_drift(self):
        deploy.prepare(self.a)
        self.current['image']='sha256:'+'4'*64
        with self.assertRaises(ValueError):deploy.activate(self.a)
        self.assertEqual(self.override.read_bytes(),self.original)
        self.current['image']=self.a.expected_image
        self.unit.write_text('synthetic unrelated unit')
        with self.assertRaises(ValueError):deploy.activate(self.a)
        self.assertEqual(self.unit.read_text(),'synthetic unrelated unit')

    def test_failed_stop_still_attempts_reader_and_nginx_recovery(self):
        deploy.prepare(self.a);deploy.activate(self.a)
        original=self.sudo
        def failing(*args,payload=None):
            if args[:2]==('systemctl','disable'):raise ValueError('synthetic stop failure')
            return original(*args,payload=payload)
        with patch.object(deploy,'sudo',side_effect=failing):
            with self.assertRaisesRegex(ValueError,'incomplete'):deploy.rollback(self.a)
        self.assertEqual(self.override.read_bytes(),self.original);self.assertEqual(self.nginx.read_text(),NGINX)
        self.assertTrue(self.env.exists());self.assertTrue((self.a.state/'ROLLBACK_INCOMPLETE').exists())

    def test_prepared_artifact_drift_stops_activation(self):
        deploy.prepare(self.a)
        (self.a.state/'unit.next').write_text('synthetic drift')
        with self.assertRaisesRegex(ValueError,'artifact changed'):deploy.activate(self.a)
        self.assertEqual(self.override.read_bytes(),self.original)

if __name__=='__main__':unittest.main()
