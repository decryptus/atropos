"""Pin disposable SQL fixtures and record only their public image identities."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / '.github' / 'sql-images.json'
BACKENDS = ('postgresql', 'mysql', 'mariadb')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
MAX_OUTPUT = 4096
INSPECT_FORMAT = '{"reference":{{json .Config.Image}},"image_id":{{json .Image}}}'


def load_lock():
    lock = json.loads(LOCK_PATH.read_text())
    if lock['schema_version'] != 1 or lock['platform'] != 'linux/amd64':
        raise ValueError('Unsupported SQL image lock')
    if set(lock['images']) != set(BACKENDS):
        raise ValueError('Expected exactly three SQL backends')
    for entry in lock['images'].values():
        if not re.fullmatch(r'[a-z0-9]+:[0-9.]+@sha256:[0-9a-f]{64}', entry['reference']):
            raise ValueError('Expected a tagged immutable image reference')
        for key in ('linux_amd64_manifest', 'linux_amd64_image_id'):
            if not DIGEST.fullmatch(entry[key]):
                raise ValueError('Expected a SHA-256 image identity')
    return lock


def pin_fixture(path, lock):
    source = path.read_bytes()
    if len(source) > 128 * 1024 or hashlib.sha256(source).hexdigest() != lock['tls_fixture']['sha256']:
        raise ValueError('TLS fixture differs from the reviewed upstream script')
    result = source
    for backend in BACKENDS:
        reference = lock['images'][backend]['reference']
        tag = reference.split('@')[0]
        original = f'image={tag};'.encode()
        if result.count(original) != 1:
            raise ValueError('Expected exactly one image assignment per backend')
        result = result.replace(original, f'image={reference};'.encode())
    # All checks precede the only write; ports, TLS setup and deadlines stay intact.
    path.write_bytes(result)


def command_output(args):
    result = subprocess.run(args, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=30)
    if len(result.stdout) > MAX_OUTPUT:
        raise ValueError('Unexpectedly large image identity output')
    return result.stdout.strip()


def inspect_container(container, expected):
    identity = json.loads(command_output([
        'docker', 'container', 'inspect', '--format', INSPECT_FORMAT, container]))
    if identity != {'reference': expected['reference'],
                    'image_id': expected['linux_amd64_image_id']}:
        raise ValueError('Running SQL fixture does not match its image lock')
    platform = command_output(['docker', 'image', 'inspect', '--format',
                               '{{.Os}}/{{.Architecture}}', identity['image_id']])
    if platform != 'linux/amd64':
        raise ValueError('SQL image qualification requires linux/amd64')
    return identity


def record(profile, lock):
    containers = []
    for backend in BACKENDS:
        if profile == 'servers':
            targets = [('server', os.environ['SQL_' + backend.upper() + '_CONTAINER'])]
        else:
            targets = [(kind, f'anysql-{kind}-{backend}')
                       for kind in ('tls', 'plain', 'expired')]
        for kind, container in targets:
            identity = inspect_container(container, lock['images'][backend])
            containers.append({'backend': backend, 'kind': kind, **identity})
    return {'schema_version': 1, 'profile': profile, 'platform': lock['platform'],
            'source_sha': command_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']),
            'lock_sha256': hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
            'containers': containers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    pin = commands.add_parser('pin-tls')
    pin.add_argument('fixture', type=Path)
    audit = commands.add_parser('record')
    audit.add_argument('profile', choices=('servers', 'tls'))
    audit.add_argument('output', type=Path)
    args = parser.parse_args()
    lock = load_lock()
    if args.operation == 'pin-tls':
        pin_fixture(args.fixture, lock)
    else:
        evidence = record(args.profile, lock)
        args.output.write_text(json.dumps(evidence, indent=2) + '\n')
        print(f"Verified {len(evidence['containers'])} SQL container image identities")


if __name__ == '__main__':
    main()
