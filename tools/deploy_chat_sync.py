"""Operator-only staged deployment; no POST, scheduler or ledger modification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
DOMAIN = 'gate-reader.gatelens.site'
SERVICE = 'gate-review-sync.service'
UNIT = Path('/etc/systemd/system') / SERVICE
ENV = Path('/etc/gate-review-sync.env')
MODULES = ('__init__.py', 'ledger.py', 'readonly.py', 'write_lock.py', 'verified_sync.py')

def run(args, payload=None):
    p = subprocess.run(args, input=payload, capture_output=True)
    if p.returncode:
        raise ValueError('Command failed; details hidden')
    return p.stdout

def sudo(*args, payload=None):
    return run(['sudo', '-n', *map(str, args)], payload)

def digest(value):
    return hashlib.sha256(value).hexdigest()

def nginx_route(text):
    # Tokenize quotes/comments/escapes, retaining offsets for a single insertion.
    pattern = r'\s+|#[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{};]|(?:\\.|[^\s{};#"\'])+'
    tokens = []
    end = 0
    for match in re.finditer(pattern, text):
        if match.start() != end:
            raise ValueError('Unsupported nginx syntax')
        end = match.end()
        word = match.group()
        if not word.isspace() and not word.startswith('#'):
            tokens.append((word, match.start()))
    if end != len(text):
        raise ValueError('Unsupported nginx syntax')
    pos = 0
    def parse(nested=False):
        nonlocal pos
        result = []
        while pos < len(tokens):
            words = []
            while pos < len(tokens) and tokens[pos][0] not in ('{', '}', ';'):
                words.append(tokens[pos][0]); pos += 1
            if pos == len(tokens):
                raise ValueError('Incomplete nginx directive')
            delimiter, offset = tokens[pos]; pos += 1
            if delimiter == '}':
                if words or not nested: raise ValueError('Invalid nginx braces')
                return result, offset
            if not words: raise ValueError('Empty nginx directive')
            children = None
            if delimiter == '{': children, close = parse(True)
            else: close = offset
            result.append((words, children, close))
        if nested: raise ValueError('Unclosed nginx block')
        return result, len(text)
    roots, _ = parse()
    candidates = []
    for words, children, close in roots:
        if words != ['server'] or children is None:
            raise ValueError('Expected standalone server configuration')
        names = [w[1:] for w, c, _ in children if w[0] == 'server_name' and c is None]
        tls = any(w[0] == 'listen' and c is None and any(re.search(r'(^|:)443$', x) for x in w[1:]) for w,c,_ in children)
        if any(DOMAIN in n for n in names) and tls: candidates.append((children, close))
    if len(candidates) != 1: raise ValueError('Expected exactly one target TLS server')
    children, close = candidates[0]
    names=[w[1:] for w,c,_ in children if w[0]=='server_name' and c is None]
    if names!=[[DOMAIN]]: raise ValueError('Unexpected TLS server aliases')
    listens=[w[1:] for w,c,_ in children if w[0]=='listen' and c is None]
    if not listens or any(not re.search(r'(^|:)443$',w[0]) or 'ssl' not in w or 'quic' in w for w in listens):
        raise ValueError('Sync route requires exclusively TLS port 443')
    allowed = {'server_name','listen','ssl_certificate','ssl_certificate_key','access_log','error_log',
               'proxy_read_timeout','proxy_connect_timeout','proxy_send_timeout','client_max_body_size',
               'add_header','ssl_dhparam','ssl_protocols','ssl_ciphers','ssl_prefer_server_ciphers',
               'ssl_session_cache','ssl_session_timeout','ssl_session_tickets'}
    for words, nested, _ in children:
        if words[0] == 'location' and nested is not None:
            if any('/review-sync' in x for x in words): raise ValueError('Existing sync route')
        elif words[0] == 'include' and words[1:] == ['/etc/letsencrypt/options-ssl-nginx.conf'] and nested is None:
            pass
        elif words[0] not in allowed or nested is not None:
            raise ValueError('Unsupported target server directive')
    route = '''
    location = /review-sync {
        client_max_body_size 128;
        client_body_timeout 5s;
        proxy_pass http://127.0.0.1:8788;
        proxy_set_header Host gate-reader.gatelens.site;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_connect_timeout 5s;
        proxy_send_timeout 15s;
        proxy_read_timeout 1800s;
        proxy_next_upstream off;
    }
'''
    return text[:close] + route + text[close:]

def unit(source, data, uid, gid):
    # Paths are validated as whitespace-free; no shell interpolation in ExecStart.
    return f'''[Unit]
Description=Explicit review synchronization
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User={uid}
Group={gid}
WorkingDirectory={source}
ExecStart=/usr/bin/python3 -m review.sync_service
EnvironmentFile={ENV}
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
Restart=no
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
BindPaths={data}:/review-data
ReadWritePaths=/review-data
[Install]
WantedBy=multi-user.target
'''

def private(path, uid, mode):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != uid or (info.st_mode & 0o777) != mode:
        raise ValueError('Private ownership/permissions mismatch')

def check_source(checkout, commit):
    if not re.fullmatch('[a-f0-9]{40}', commit): raise ValueError('Invalid commit')
    listing = run(['git','-C',str(checkout),'ls-tree','-r',commit]).decode()
    if not listing: raise ValueError('Missing source tree')
    if '\ttools/deploy_chat_sync.py\n' not in listing: raise ValueError('Commit lacks deployment tool')
    for line in listing.splitlines():
        meta, name = line.split('\t', 1)
        mode, kind, sha = meta.split()
        p = SOURCE / name
        if mode != '100644' or kind != 'blob' or p.is_symlink(): raise ValueError('Unsupported source entry')
        if run(['git','-C',str(checkout),'hash-object','--stdin'],p.read_bytes()).decode().strip() != sha:
            raise ValueError('Release differs from approved commit')

def validate_reader(cfg, current, reader, data, expected, uid, gid):
    if set(cfg['services']) != {'reader'}: raise ValueError('Unexpected compose services')
    s = cfg['services']['reader']; b = s.get('build')
    if not isinstance(b,dict) or set(b) - {'context','dockerfile'}:
        raise ValueError('Unsupported build extensions')
    context = Path(b['context']); dockerfile = b.get('dockerfile','Dockerfile')
    # Only the old installer's isolated code context; never the .env directory.
    expected_context = reader.parent / ('.'+reader.name+'-bridge-install') / 'build'
    if context != expected_context or dockerfile != 'Dockerfile': raise ValueError('Unexpected reader build context')
    ports = s.get('ports',[])
    if len(ports)!=1 or ports[0].get('host_ip')!='127.0.0.1' or str(ports[0].get('published'))!='8787' or ports[0].get('target')!=8787:
        raise ValueError('Unexpected reader port')
    mounts = current.get('mounts',[])
    if current.get('image') != expected or current.get('user') != f'{uid}:{gid}' or len(mounts)!=1:
        raise ValueError('Unexpected current container')
    m = mounts[0]
    if m.get('Type')!='bind' or m.get('Source')!=str(data) or m.get('Destination')!='/review-data' or m.get('RW') is not False:
        raise ValueError('Reader mount must remain read-only')
    if current.get('privileged') or current.get('caps') or current.get('pid') or current.get('userns'):
        raise ValueError('Unexpected reader privileges')
    if s.get('user')!=f'{uid}:{gid}' or s.get('privileged') or s.get('cap_add') or s.get('network_mode') or s.get('pid') or s.get('group_add'):
        raise ValueError('Unexpected compose access')
    volumes=s.get('volumes',[])
    if len(volumes)!=1 or volumes[0].get('source')!=str(data) or volumes[0].get('target')!='/review-data' or volumes[0].get('read_only') is not True:
        raise ValueError('Unexpected compose mounts')
    return context

def compose(reader, override, *args):
    return sudo('docker','compose','-f',reader/'compose.yaml','-f',override,*args)

def put_root(source, target, mode):
    temporary=Path(sudo('mktemp',str(target.parent / ('.'+target.name+'.review-XXXXXX'))).decode().strip())
    try:
        sudo('install','-o','root','-g','root','-m',mode,source,temporary)
        sudo('mv','-T',temporary,target)
    finally:
        sudo('rm','-f',temporary)

def put_private(target, value):
    import tempfile
    fd, name=tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(value);out.flush();os.fsync(out.fileno())
        os.replace(name,target)
        fd=os.open(target.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally: Path(name).unlink(missing_ok=True)

def worker_health():
    import http.client
    import time
    for _ in range(30):
        c=http.client.HTTPConnection('127.0.0.1',8788,timeout=1)
        try:
            c.request('GET','/review-sync');r=c.getresponse();r.read(2048)
            if r.status==405: return
            raise ValueError('Unexpected worker response')
        except OSError: time.sleep(.2)
        finally: c.close()
    raise ValueError('Worker unavailable')

def reader_smoke():
    import http.client
    values=sudo('cat',ENV).decode().splitlines()
    readers=[v.split('=',1)[1] for v in values if v.startswith('READER_TOKEN=')]
    if len(readers)!=1: raise ValueError('Credential file unavailable')
    c=http.client.HTTPConnection('127.0.0.1',8787,timeout=15)
    try:
        c.request('GET','/review-summary?limit=1',headers={'Authorization':'Bearer '+readers[0]})
        r=c.getresponse();body=r.read(512*1024+1)
        if r.status!=200 or len(body)>512*1024: raise ValueError('Read-only summary unavailable')
        data=json.loads(body)
        if data.get('ok') is not True or 'last_verified_sync' not in data.get('data',{}):
            raise ValueError('Marker-aware reader unavailable')
    finally: c.close()

def prepare(a):
    if os.getuid()!=a.uid or a.uid==0 or a.gid==0: raise ValueError('Use approved non-root owner')
    for p in (SOURCE,a.reader,a.data,a.state,a.nginx,a.checkout):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(p)) or p.is_symlink(): raise ValueError('Unsafe path')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',a.expected_image): raise ValueError('Invalid expected image')
    check_source(a.checkout,a.commit)
    private(a.data,a.uid,0o700);private(a.data/'ledger.sqlite',a.uid,0o600)
    if a.state.exists() or UNIT.exists() or ENV.exists(): raise ValueError('Existing deployment state; stop')
    if a.state==a.data or a.data in a.state.parents or SOURCE in a.state.parents: raise ValueError('Unsafe state location')
    original = sudo('cat',a.nginx)
    candidate = nginx_route(original.decode()).encode()
    override = a.reader/'compose.override.yaml'
    cfg = json.loads(compose(a.reader,override,'config','--format','json'))
    cid = compose(a.reader,override,'ps','-q','reader').decode().strip()
    if not re.fullmatch('[a-f0-9]{12,64}',cid): raise ValueError('Expected one reader container')
    fmt = '{"image":{{json .Image}},"user":{{json .Config.User}},"mounts":{{json .Mounts}},"privileged":{{json .HostConfig.Privileged}},"caps":{{json .HostConfig.CapAdd}},"pid":{{json .HostConfig.PidMode}},"userns":{{json .HostConfig.UsernsMode}}}'
    current=json.loads(sudo('docker','inspect','--format',fmt,cid))
    context=validate_reader(cfg,current,a.reader,a.data,a.expected_image,a.uid,a.gid)
    # Validate original base Dockerfile with the existing reviewed parser.
    from tools.install_reader_bridge import dockerfile_instructions
    base=(context.parent/'backup'/'Dockerfile').read_text()
    instructions=dockerfile_instructions(base)
    for required in ('FROM python:3.12-slim','WORKDIR /app','COPY app.py /app/app.py','USER 65534:65534'):
        if instructions.count(required)!=1: raise ValueError('Original Dockerfile mismatch')
    # Credentials are captured privately, only READER_TOKEN retained. Never logged.
    values=json.loads(sudo('docker','inspect','--format','{{json .Config.Env}}',cid))
    readers=[v.split('=',1)[1] for v in values if v.startswith('READER_TOKEN=')]
    if len(readers)!=1 or not re.fullmatch('[A-Za-z0-9_-]{32,128}',readers[0]): raise ValueError('Reader credential configuration unsupported')
    import socket
    with socket.socket() as sock: sock.bind(('127.0.0.1',8788))
    os.umask(0o077);a.state.mkdir(mode=0o700)
    (a.state/'nginx.original').write_bytes(original)
    (a.state/'nginx.next').write_bytes(candidate)
    (a.state/'override.original').write_bytes(override.read_bytes())
    build=a.state/'build';build.mkdir();(build/'review').mkdir()
    shutil.copyfile(SOURCE/'reader_bridge'/'app.py',build/'app.py')
    for name in MODULES: shutil.copyfile(SOURCE/'review'/name,build/'review'/name)
    for p in (build/'app.py',*(build/'review').iterdir()): p.chmod(0o644)
    (build/'review').chmod(0o755)
    (build/'Dockerfile').write_text(base+'\nCOPY '+' '.join('review/'+x for x in MODULES)+' /app/review/\n')
    # Original override must be the old installer's JSON layout; preserve all keys.
    old=json.loads(override.read_bytes())
    if set(old)!= {'services'} or set(old['services'])!={'reader'}: raise ValueError('Unexpected override layout')
    tag='review-chat-reader:'+a.commit[:16]
    old['services']['reader']['image']=tag
    old['services']['reader']['build']={'context':str(build),'dockerfile':'Dockerfile'}
    (a.state/'override.next').write_text(json.dumps(old,indent=2))
    (a.state/'unit.next').write_text(unit(SOURCE,a.data,a.uid,a.gid))
    rollback_tag='review-chat-rollback:'+a.expected_image.split(':')[1][:16]
    manifest={'reader':str(a.reader),'nginx':str(a.nginx),'old_image':rollback_tag,'commit':a.commit,'checkout':str(a.checkout),
              'base_sha':digest((a.reader/'compose.yaml').read_bytes()),'phase':'prepared',
              'data':str(a.data),'uid':a.uid,'gid':a.gid,'expected_image':a.expected_image,'new_tag':tag,
              'artifacts':{name:digest((a.state/name).read_bytes()) for name in
                 ('nginx.original','nginx.next','override.original','override.next','unit.next')}}
    (a.state/'manifest.json').write_text(json.dumps(manifest))
    for path in a.state.iterdir():
        if path.is_file():
            fd=os.open(path,os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    fd=os.open(a.state,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    sudo('docker','image','tag',a.expected_image,rollback_tag)
    compose(a.reader,a.state/'override.next','build','reader')
    manifest['new_image']=sudo('docker','image','inspect','--format','{{.Id}}',tag).decode().strip()
    if not re.fullmatch('sha256:[a-f0-9]{64}',manifest['new_image']): raise ValueError('Built image unavailable')
    # Credentials are only created after successful build; not placed in context.
    payload=('READER_TOKEN='+readers[0]+'\nREVIEW_SYNC_TOKEN='+secrets.token_hex(32)+'\n').encode()
    sudo('install','-o','root','-g','root','-m','600','/dev/stdin',ENV,payload=payload)
    manifest['env_sha']=digest(payload)
    put_private(a.state/'manifest.json',json.dumps(manifest).encode())
    (a.state/'READY').touch()

def state(a):
    private(a.state,os.getuid(),0o700)
    m=json.loads((a.state/'manifest.json').read_text())
    for name,sha in m['artifacts'].items():
        if name not in ('nginx.original','nginx.next','override.original','override.next','unit.next'):
            raise ValueError('Unexpected state artifact')
        private(a.state/name,os.getuid(),0o600)
        if digest((a.state/name).read_bytes())!=sha: raise ValueError('Prepared artifact changed')
    reader=Path(m['reader']); nginx=Path(m['nginx'])
    if digest((reader/'compose.yaml').read_bytes())!=m['base_sha']: raise ValueError('Base compose changed')
    return m,reader,nginx

def compatible(current, original, next_value):
    if current not in (original,next_value): raise ValueError('Configuration changed outside deployment; stop')

def rollback(a):
    m,reader,nginx=state(a)
    override=reader/'compose.override.yaml'
    compatible(override.read_bytes(),(a.state/'override.original').read_bytes(),(a.state/'override.next').read_bytes())
    compatible(sudo('cat',nginx),(a.state/'nginx.original').read_bytes(),(a.state/'nginx.next').read_bytes())
    if UNIT.exists() and sudo('cat',UNIT)!=(a.state/'unit.next').read_bytes(): raise ValueError('Unit changed; stop')
    if ENV.exists() and digest(sudo('cat',ENV))!=m.get('env_sha'): raise ValueError('Credential file changed; stop')
    failures=[]
    def attempt(action):
        try: action();return True
        except Exception: failures.append(True);return False
    stopped=True
    if UNIT.exists(): stopped=attempt(lambda:sudo('systemctl','disable','--now',SERVICE))
    def restore_nginx():
        put_root(a.state/'nginx.original',nginx,'644')
        sudo('nginx','-t');sudo('systemctl','reload','nginx')
    attempt(restore_nginx)
    # Preserve original override bytes; explicit third file pins the retained image.
    def restore_reader():
        put_private(override,(a.state/'override.original').read_bytes())
        pinned=a.state/'rollback-image.json'
        pinned.write_text(json.dumps({'services':{'reader':{'image':m['expected_image'],'pull_policy':'never'}}}))
        sudo('docker','compose','-f',reader/'compose.yaml','-f',override,'-f',pinned,
             'up','-d','--no-build','--no-deps','--force-recreate','reader')
    attempt(restore_reader)
    if stopped:
        attempt(lambda:sudo('rm','-f',UNIT,ENV))
        attempt(lambda:sudo('systemctl','daemon-reload'))
    if failures:
        (a.state/'ROLLBACK_INCOMPLETE').touch()
        raise ValueError('Rollback incomplete; private state retained, inspect services locally')
    (a.state/'ROLLED_BACK').touch()

def activate(a):
    m,reader,nginx=state(a)
    check_source(Path(m['checkout']),m['commit'])
    if not (a.state/'READY').exists() or (a.state/'ACTIVE').exists() or (a.state/'ROLLED_BACK').exists(): raise ValueError('Invalid activation state')
    override=reader/'compose.override.yaml'
    if override.read_bytes()!=(a.state/'override.original').read_bytes() or sudo('cat',nginx)!=(a.state/'nginx.original').read_bytes():
        raise ValueError('Configuration changed since preparation')
    if UNIT.exists() or not ENV.exists() or digest(sudo('cat',ENV))!=m.get('env_sha'):
        raise ValueError('Unit/credential state changed since preparation')
    if sudo('stat','-c','%u:%a',ENV).decode().strip()!='0:600': raise ValueError('Unsafe credential permissions')
    if sudo('docker','image','inspect','--format','{{.Id}}',m['new_tag']).decode().strip()!=m['new_image']:
        raise ValueError('Prepared image tag changed')
    cfg=json.loads(compose(reader,override,'config','--format','json'))
    cid=compose(reader,override,'ps','-q','reader').decode().strip()
    if not re.fullmatch('[a-f0-9]{12,64}',cid): raise ValueError('Reader changed')
    fmt='{"image":{{json .Image}},"user":{{json .Config.User}},"mounts":{{json .Mounts}},"privileged":{{json .HostConfig.Privileged}},"caps":{{json .HostConfig.CapAdd}},"pid":{{json .HostConfig.PidMode}},"userns":{{json .HostConfig.UsernsMode}}}'
    validate_reader(cfg,json.loads(sudo('docker','inspect','--format',fmt,cid)),reader,Path(m['data']),m['expected_image'],m['uid'],m['gid'])
    import socket
    with socket.socket() as sock: sock.bind(('127.0.0.1',8788))
    try:
        put_private(override,(a.state/'override.next').read_bytes())
        compose(reader,override,'up','-d','--no-build','--no-deps','--force-recreate','reader')
        from tools.install_reader_bridge import health
        health()
        reader_smoke()
        put_root(a.state/'unit.next',UNIT,'644')
        sudo('systemd-analyze','verify',UNIT)
        sudo('systemctl','daemon-reload');sudo('systemctl','enable','--now',SERVICE)
        sudo('systemctl','is-active','--quiet',SERVICE)
        worker_health()
        put_root(a.state/'nginx.next',nginx,'644')
        sudo('nginx','-t');sudo('systemctl','reload','nginx')
        (a.state/'ACTIVE').touch()
    except BaseException:
        rollback(a)
        raise ValueError('Activation failed; rollback completed') from None

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','activate','rollback'))
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--reader-dir',type=Path);p.add_argument('--data-dir',type=Path)
    p.add_argument('--nginx-file',type=Path);p.add_argument('--checkout',type=Path)
    p.add_argument('--commit');p.add_argument('--expected-image');p.add_argument('--uid',type=int);p.add_argument('--gid',type=int)
    a=p.parse_args();a.state=a.state_dir;a.reader=a.reader_dir;a.data=a.data_dir;a.nginx=a.nginx_file
    try:
        if a.action=='prepare':
            if any(x is None for x in (a.reader,a.data,a.nginx,a.checkout,a.commit,a.expected_image,a.uid,a.gid)): raise ValueError('Missing preparation arguments')
            prepare(a)
        elif a.action=='activate': activate(a)
        else: rollback(a)
        print(a.action.upper()+' PASS; no production sync requested')
        return 0
    except (Exception,KeyboardInterrupt):
        print('Deployment FAIL; details hidden. Do not retry automatically; retain private state.',file=sys.stderr)
        return 1

if __name__=='__main__':
    sys.path.insert(0,str(SOURCE));sys.exit(main())
