"""Exercise the installed image under the documented non-root restrictions."""
import json
from pathlib import Path
import subprocess
import tomllib
import uuid

IMAGE = 'atropos:test'
ROOT = Path.cwd()
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
HARDENING = ['--network', 'none', '--read-only', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges', '--pids-limit', '128', '--memory', '512m',
    '--cpus', '2', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700']


def run(arguments, *, python=False, volume=None, capture=False):
    command = ['docker', 'run', '--rm', *HARDENING,
        '--mount', 'type=bind,src=' + str(ROOT) + ',dst=/acceptance,readonly', '-w', '/tmp']
    if volume:
        command += ['--mount', 'type=volume,src=' + volume + ',dst=/var/lib/atropos',
            '-e', 'ATROPOS_STORAGE_BACKEND=sqlite', '-e', 'ATROPOS_STORAGE_URL=sqlite:////var/lib/atropos/state.db',
            '-e', 'ATROPOS_QUEUE_BACKEND=storage']
    if python:
        command += ['--entrypoint', 'python']
    result = subprocess.run(command + [IMAGE, *arguments], check=True, timeout=180,
        capture_output=capture, text=True)
    return result.stdout


run(['-c', "import os,importlib.metadata; assert os.getuid()==os.getgid()==10001; assert importlib.metadata.version('atraxis-atropos')==" + repr(VERSION)], python=True)
run(['-m', 'pip', 'check'], python=True)
run(['--help'])
run(['/acceptance/scripts/check-test-collection.py', '--installed', 'installed_tests/service'], python=True)
run(['-m', 'unittest', 'discover', '-s', '/acceptance/installed_tests/service', '-v'], python=True)
volume = 'atropos-image-check-' + uuid.uuid4().hex
subprocess.run(['docker', 'volume', 'create', volume], check=True, timeout=30)
try:
    submit = ['submit', '/acceptance/examples/inspect.yml', '--target', 'synthetic-image', '--dedup-key', 'image-one']
    admitted = json.loads(run(submit, volume=volume, capture=True))
    for _ in range(2):
        run(['worker', '--once'], volume=volume)
    result = json.loads(run(['status', admitted['id']], volume=volume, capture=True))
    assert result['status'] == 'succeeded'
    assert json.loads(run(submit, volume=volume, capture=True))['id'] == admitted['id']
    print('State and duplicate refusal survived container recreation')
finally:
    subprocess.run(['docker', 'volume', 'rm', volume], check=True, timeout=30)
