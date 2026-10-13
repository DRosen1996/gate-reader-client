"""Manually invoked installer; private staging, preflight, build-first, rollback."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

BASE_SHA = 'd3623009ee66c3d3f6b3f32118486af986228c987b8c39ba1355cdd224d31414'
SOURCE = Path(__file__).resolve().parents[1]
SUDO_DOCKER = False
STAGE = "ARGUMENTS"

def stage(name):
    global STAGE
    STAGE = name


def run(args):
    command = ['sudo', '-n', *args] if SUDO_DOCKER and args and args[0] == 'docker' else args
    p = subprocess.run(command, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError('Command failed; details hidden')
    return p.stdout.strip()


def private_dir(path, uid):
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Invalid private directory')
    info = path.stat()
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Private directory permissions mismatch')


def dockerfile_instructions(text):
    """Only the existing single-stage layout, with default escape continuations."""
    logical, parts = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#'):
            if re.match(r'#\s*syntax\s*=', line, re.I):
                raise ValueError('Dockerfile mismatch')
            if re.match(r'#\s*escape\s*=', line, re.I) and line.split('=', 1)[1].strip() != chr(92):
                raise ValueError('Dockerfile mismatch')
            continue
        if not line:
            continue
        slash_count = len(line) - len(line.rstrip(chr(92)))
        if slash_count > 1:
            raise ValueError('Dockerfile mismatch')
        continued = slash_count == 1
        parts.append(line[:-1].rstrip() if continued else line)
        if continued:
            continue
        joined = ' '.join(parts)
        keyword, *body = joined.split(None, 1)
        if not body:
            raise ValueError('Dockerfile mismatch')
        logical.append(keyword.upper() + ' ' + body[0])
        parts = []
    if parts:
        raise ValueError('Dockerfile mismatch')
    names = [line.split(None, 1)[0] for line in logical]
    allowed = {'FROM', 'WORKDIR', 'ENV', 'COPY', 'USER', 'EXPOSE', 'HEALTHCHECK', 'CMD'}
    if any(name not in allowed for name in names):
        raise ValueError('Unrecognized Dockerfile instruction')
    # Optional ENV/EXPOSE/HEALTHCHECK are preserved existing instructions,
    # not supplied by installer arguments or synthesized into the Dockerfile.
    expected = ['FROM', 'WORKDIR'] + (['ENV'] if 'ENV' in names else []) + ['COPY', 'USER'] + (['EXPOSE'] if 'EXPOSE' in names else []) + (['HEALTHCHECK'] if 'HEALTHCHECK' in names else []) + ['CMD']
    if names != expected:
        raise ValueError('Dockerfile mismatch')
    return logical


def preflight(reader, ledger, service, uid, gid, image):
    stage("PREFLIGHT_FILES_PERMISSIONS")
    if os.getuid() != uid or uid == 0 or gid == 0:
        raise ValueError('Run as approved non-root owner')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('Invalid expected image')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', service):
        raise ValueError('Invalid service')
    if reader.is_symlink() or ledger.is_symlink():
        raise ValueError('Symlink directory refused')
    reader, ledger = reader.resolve(), ledger.resolve()
    state = reader.parent / ('.' + reader.name + '-bridge-install')
    if state.exists() or state.is_symlink() or (reader / 'compose.override.yaml').exists() or (reader / 'compose.override.yml').exists():
        raise ValueError('Already installed or override exists; stop')
    if reader == SOURCE or reader in SOURCE.parents or SOURCE in reader.parents or ledger == SOURCE or SOURCE in ledger.parents or ledger == reader or reader in ledger.parents:
        raise ValueError('Reader context must be outside checkout')
    for name in ('app.py','Dockerfile','compose.yaml'):
        if (reader/name).is_symlink() or not (reader/name).is_file():
            raise ValueError('Reader file missing or symlink')
    private_dir(ledger, uid)
    info = (ledger / 'ledger.sqlite').lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Ledger permissions mismatch')
    if ledger.stat().st_gid != gid:
        raise ValueError('Ledger group mismatch')
    # Use the bounded read-only adapter without inspecting any account output.
    stage("PREFLIGHT_LEDGER")
    from review import readonly
    original = readonly.DATABASE
    try:
        readonly.DATABASE = ledger / 'ledger.sqlite'
        readonly.query_review('/review-summary?limit=1')
    finally:
        readonly.DATABASE = original
    stage("PREFLIGHT_SOURCE")
    if hashlib.sha256((reader / 'app.py').read_bytes()).hexdigest() != BASE_SHA:
        raise ValueError('Reader source mismatch')
    dockerfile = (reader / 'Dockerfile').read_bytes().decode('utf-8')
    instructions = dockerfile_instructions(dockerfile)
    for required in ('FROM python:3.12-slim', 'WORKDIR /app', 'COPY app.py /app/app.py', 'USER 65534:65534'):
        if instructions.count(required) != 1:
            raise ValueError('Dockerfile mismatch')
    if sum(line.startswith('COPY ') for line in instructions) != 1:
        raise ValueError('Unexpected COPY')
    stage("PREFLIGHT_DOCKER_CONFIG")
    cfg = json.loads(run(['docker', 'compose', '-f', str(reader/'compose.yaml'), 'config', '--format', 'json']))
    s = cfg['services'][service]
    b = s['build']
    if not isinstance(b, dict) or Path(b['context']).resolve() != reader or b.get('dockerfile', 'Dockerfile') != 'Dockerfile':
        raise ValueError('Build layout mismatch')
    if any(b.get(k) for k in ('args','secrets','ssh','additional_contexts','privileged')) or s.get('pull_policy') not in (None, 'missing', 'if_not_present', 'never'):
        raise ValueError('Unexpected build or pull policy')
    if s.get('user') or s.get('volumes') or s.get('privileged') or s.get('cap_add') or s.get('group_add') or s.get('network_mode') or s.get('pid') or s.get('userns_mode'):
        raise ValueError('Unexpected service access')
    ports = s.get('ports', [])
    if len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1' or str(ports[0].get('published')) != '8787' or ports[0].get('target') != 8787 or ports[0].get('protocol') != 'tcp':
        raise ValueError('Port mismatch')
    if 'no-new-privileges:true' not in s.get('security_opt', []):
        raise ValueError('Missing no-new-privileges')
    stage("PREFLIGHT_CONTAINER")
    cid = run(['docker','compose','-f',str(reader/'compose.yaml'),'ps','-q',service])
    if not re.fullmatch(r'[a-f0-9]{12,64}', cid):
        raise ValueError('Expected one running container')
    fmt = '{"image":{{json .Image}},"user":{{json .Config.User}},"mounts":{{json .Mounts}},"privileged":{{json .HostConfig.Privileged}},"userns":{{json .HostConfig.UsernsMode}},"groups":{{json .HostConfig.GroupAdd}},"caps":{{json .HostConfig.CapAdd}},"pid":{{json .HostConfig.PidMode}},"security":{{json .HostConfig.SecurityOpt}}}'
    current = json.loads(run(['docker','inspect','--format',fmt,cid]))
    if current['image'] != image or current['user'] != '65534:65534' or any(current[k] for k in ('mounts','privileged','userns','groups','caps','pid')) or 'no-new-privileges:true' not in current['security']:
        raise ValueError('Running container mismatch')
    stage("PREFLIGHT_DOCKER_INFO")
    options = json.loads(run(['docker','info','--format','{{json .SecurityOptions}}']))
    if any('rootless' in x or 'userns' in x for x in options):
        raise ValueError('User mapping unsupported')
    for rel in ('reader_bridge/app.py', 'review/__init__.py', 'review/ledger.py', 'review/readonly.py','review/write_lock.py','review/verified_sync.py'):
        if not (SOURCE/rel).is_file():
            raise ValueError('Missing source')
    return reader, ledger, state, dockerfile


def rollback(state):
    private_dir(state, os.getuid())
    m = json.loads((state/'manifest.json').read_text())
    reader = Path(m['reader'])
    for name in ('app.py','Dockerfile','compose.yaml'):
        if (reader/name).read_bytes() != (state/'backup'/name).read_bytes():
            raise ValueError('Original files changed; manual review required')
    # Original files were never modified; only remove our exact override.
    override = reader/'compose.override.yaml'
    if override.exists():
        if hashlib.sha256(override.read_bytes()).hexdigest() != m['override_sha']:
            raise ValueError('Override changed; manual review required')
        override.unlink()
    rollback_file = state/'rollback.json'
    rollback_file.write_text(json.dumps({'services': {m['service']: {'image': m['old_image'], 'pull_policy':'never'}}}))
    run(['docker','compose','-f',str(reader/'compose.yaml'),'-f',str(rollback_file),'up','-d','--no-build','--no-deps','--force-recreate',m['service']])
    (state/'ROLLED_BACK').touch()


def health():
    for attempt in range(30):
        conn = http.client.HTTPConnection('127.0.0.1', 8787, timeout=1)
        try:
            conn.request('GET', '/health')
            response = conn.getresponse()
            body = response.read(8193)
            if response.status == 200 and len(body) <= 8192 and json.loads(body).get('ok') is True:
                return
        except Exception:
            pass
        finally:
            conn.close()
        time.sleep(0.2)
    raise RuntimeError('Health check failed')


def install(args):
    reader, ledger, state, dockerfile = preflight(Path(args.reader_dir), Path(args.ledger_dir), args.service, args.uid, args.gid, args.expected_image)
    os.umask(0o077)
    state.mkdir(mode=0o700)
    build = state/'build'; build.mkdir(mode=0o700)
    backup = state/'backup'; backup.mkdir(mode=0o700)
    for name in ('app.py','Dockerfile','compose.yaml'):
        shutil.copyfile(reader/name, backup/name)
    shutil.copyfile(SOURCE/'reader_bridge/app.py', build/'app.py')
    (build/'review').mkdir()
    for name in ('__init__.py','ledger.py','readonly.py','write_lock.py','verified_sync.py'):
        shutil.copyfile(SOURCE/'review'/name, build/'review'/name)
    # Only these code files enter the build context; no .env or account files.
    for path in (build/'app.py', *list((build/'review').iterdir())):
        path.chmod(0o644)
    (build/'review').chmod(0o755)
    (build/'Dockerfile').write_text(dockerfile + '\nCOPY review/__init__.py review/ledger.py review/readonly.py review/write_lock.py review/verified_sync.py /app/review/\n', encoding='utf-8')
    image_digest = hashlib.sha256(b''.join((build/rel).read_bytes() for rel in ('app.py','review/__init__.py','review/ledger.py','review/readonly.py','review/write_lock.py','review/verified_sync.py'))).hexdigest()[:16]
    override_data = json.dumps({'services': {args.service: {
        'image': 'review-reader-bridge:' + image_digest,
        'user': str(args.uid)+':'+str(args.gid),
        'build': {'context': str(build), 'dockerfile': 'Dockerfile'},
        'volumes': [{'type':'bind','source':str(ledger),'target':'/review-data','read_only':True}]
    }}}, indent=2)
    staged = state/'override.json'; staged.write_text(override_data)
    m = {'reader':str(reader),'service':args.service,'old_image':args.expected_image,
         'override_sha':hashlib.sha256(override_data.encode()).hexdigest()}
    rollback_tag = 'review-reader-rollback:' + args.expected_image.split(':')[-1][:12]
    run(['docker','image','tag',args.expected_image,rollback_tag])
    m['old_image'] = rollback_tag
    (state/'manifest.json').write_text(json.dumps(m))
    base = ['docker','compose','-f',str(reader/'compose.yaml'),'-f',str(staged)]
    # A failed build leaves the running service and default Compose unchanged.
    run(base+['build',args.service])
    override = reader/'compose.override.yaml'
    with override.open('x') as out:
        out.write(override_data)
    try:
        run(['docker','compose','-f',str(reader/'compose.yaml'),'-f',str(override),'up','-d','--no-build','--no-deps','--force-recreate',args.service])
        health()
    except BaseException:
        rollback(state)
        raise RuntimeError('Recreate failed; rollback completed') from None
    (state/'INSTALLED').touch()
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback')
    parser.add_argument('--sudo-docker', action='store_true', help='Only Docker children use sudo -n; first authenticate sudo in the terminal')
    parser.add_argument('--reader-dir'); parser.add_argument('--ledger-dir')
    parser.add_argument('--service', default='reader')
    parser.add_argument('--uid',type=int); parser.add_argument('--gid',type=int)
    parser.add_argument('--expected-image')
    args=parser.parse_args()
    global SUDO_DOCKER
    SUDO_DOCKER = args.sudo_docker
    try:
        if args.rollback:
            os.umask(0o077); rollback(Path(args.rollback)); print('Rollback PASS')
        else:
            if any(x is None for x in (args.reader_dir,args.ledger_dir,args.uid,args.gid,args.expected_image)):
                raise ValueError('Missing installation argument')
            state=install(args); print('Install PASS; private recovery directory: '+str(state))
        return 0
    except (Exception, KeyboardInterrupt):
        print('Operation FAIL; details hidden. No automatic retry. Check private recovery state.',file=sys.stderr)
        return 1

if __name__=='__main__':
    sys.path.insert(0,str(SOURCE))
    sys.exit(main())
