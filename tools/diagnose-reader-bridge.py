"""Read-only installer preflight; fixed output, no install or credential prompt."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import install_reader_bridge as installer

ERRORS = {
    'Run as approved non-root owner':'E_OWNER',
    'Invalid expected image':'E_IMAGE_ARGUMENT', 'Invalid service':'E_SERVICE_ARGUMENT',
    'Symlink directory refused':'E_DIRECTORY_SYMLINK',
    'Already installed or override exists; stop':'E_EXISTING_STATE_OR_OVERRIDE',
    'Reader context must be outside checkout':'E_DIRECTORY_LAYOUT',
    'Reader file missing or symlink':'E_READER_FILES',
    'Invalid private directory':'E_PRIVATE_DIRECTORY',
    'Private directory permissions mismatch':'E_DIRECTORY_PERMISSIONS',
    'Ledger permissions mismatch':'E_LEDGER_PERMISSIONS',
    'Ledger group mismatch':'E_LEDGER_GROUP',
    'Reader source mismatch':'E_SOURCE_HASH',
    'Unrecognized Dockerfile instruction':'E_DOCKERFILE_INSTRUCTION',
    'Dockerfile mismatch':'E_DOCKERFILE_LAYOUT', 'Unexpected COPY':'E_DOCKERFILE_COPY',
    'Build layout mismatch':'E_BUILD_LAYOUT',
    'Unexpected build or pull policy':'E_BUILD_POLICY',
    'Unexpected service access':'E_SERVICE_ACCESS', 'Port mismatch':'E_PORT',
    'Missing no-new-privileges':'E_SECURITY_OPTION',
    'Expected one running container':'E_CONTAINER_COUNT',
    'Running container mismatch':'E_CONTAINER_STATE',
    'User mapping unsupported':'E_USER_MAPPING', 'Missing source':'E_SOURCE_FILES',
}


def diagnose(args):
    state = Path(args.reader_dir).resolve().parent / ('.' + Path(args.reader_dir).name + '-bridge-install')
    try:
        print('RECOVERY_STATE=' + ('YES' if state.exists() or state.is_symlink() else 'NO'))
    except Exception:
        print('RECOVERY_STATE=UNKNOWN')
    original, original_sudo = installer.run, installer.SUDO_DOCKER
    def read_only_run(command):
        compose_read = (len(command) >= 5 and command[:2] == ['docker','compose'] and
            (command[-3:] == ['config','--format','json'] or command[-3:] == ['ps','-q',args.service]))
        inspect_read = command[:3] == ['docker','inspect','--format']
        info_read = command[:3] == ['docker','info','--format']
        if not (compose_read or inspect_read or info_read):
            raise RuntimeError('Diagnostic command refused')
        return original(command)
    installer.run, installer.SUDO_DOCKER = read_only_run, args.sudo_docker
    try:
        installer.preflight(Path(args.reader_dir),Path(args.ledger_dir),args.service,args.uid,args.gid,args.expected_image)
        print('PREFLIGHT=PASS; NO_INSTALL')
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        code = ERRORS.get(str(exc), 'E_VALIDATION') if type(exc) is ValueError else 'E_INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'E_READ_OPERATION'
        print('PREFLIGHT=FAIL CHECK=' + installer.STAGE + ' CODE=' + code)
        return 1
    finally:
        installer.run, installer.SUDO_DOCKER = original, original_sudo


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reader-dir',required=True);p.add_argument('--ledger-dir',required=True)
    p.add_argument('--service',default='reader');p.add_argument('--uid',type=int,required=True)
    p.add_argument('--gid',type=int,required=True);p.add_argument('--expected-image',required=True)
    p.add_argument('--sudo-docker',action='store_true')
    return diagnose(p.parse_args())

if __name__ == '__main__':
    sys.exit(main())
