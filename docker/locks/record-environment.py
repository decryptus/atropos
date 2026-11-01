"""Verify the fixed Compose build inputs and record the installed environment."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

LOCKS = Path('/locks')
BUILD_RECORD = Path('/build-record.json')
ENVIRONMENT_RECORD = Path('/usr/local/share/atraxis/environment.json')
APT_SOURCES = Path('/etc/apt/sources.list.d/debian.sources')
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_ARCHIVES = 128
CHUNK_BYTES = 1024 * 1024
PROFILES = frozenset({'web', 'engine', 'proxy'})


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def verify_versions(actual, expected):
    if {canonical(k): v for k, v in actual.items()} != {
            canonical(k): v for k, v in expected.items()}:
        raise ValueError('compose_environment_versions_mismatch')


def file_identity(path, limit=MAX_ARCHIVE_BYTES):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('compose_environment_file_invalid')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return dict(sha256=digest.hexdigest(), size=info.st_size)


def file_set(directory):
    paths = sorted(directory.iterdir())
    if not paths or len(paths) > MAX_ARCHIVES:
        raise ValueError('compose_environment_file_set_invalid')
    return {p.name: file_identity(p) for p in paths}


def locked_downloads(directory, requirements):
    text = requirements.read_text()
    allowed = set(re.findall(r'--hash=sha256:([0-9a-f]{64})', text))
    count = len(re.findall(r'^[A-Za-z0-9_.-]+==[^\s]+', text, re.MULTILINE))
    files = file_set(directory)
    if len(files) != count or any(f['sha256'] not in allowed for f in files.values()):
        raise ValueError('compose_environment_archive_mismatch')
    return files


def verify_wheels(directory, expected):
    if file_set(directory) != expected:
        raise ValueError('compose_environment_wheels_mismatch')


def write_record(path, record):
    text = json.dumps(record, indent=2, sort_keys=True) + '\n'
    if len(text.encode()) > MAX_RECORD_BYTES:
        raise ValueError('compose_environment_record_too_large')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {'build', 'installed'} or sys.argv[2] not in PROFILES:
        raise ValueError('compose_environment_arguments_invalid')
    phase, profile = sys.argv[1:]
    spec = json.loads((LOCKS / 'versions.json').read_text())
    if platform.python_version() != spec['python']:
        raise ValueError('compose_environment_python_mismatch')
    actual = {d.metadata['Name']: d.version for d in metadata.distributions()}
    if phase == 'build':
        if profile == 'proxy':
            raise ValueError('compose_environment_build_profile_invalid')
        verify_versions(actual, spec['build'])
        write_record(BUILD_RECORD, dict(
            profile=profile, python=platform.python_version(), builder_packages=actual,
            build_tools=locked_downloads(Path('/build-tools'), LOCKS / 'build.txt'),
            dependency_inputs=locked_downloads(Path('/sources'), LOCKS / (profile + '.txt')),
            wheels=file_set(Path('/wheels'))))
        return
    verify_versions(actual, spec['profiles'][profile])
    if APT_SOURCES.read_bytes() != (LOCKS / 'debian.sources').read_bytes():
        raise ValueError('compose_environment_apt_sources_mismatch')
    native = subprocess.check_output(['dpkg-query', '-W',
        '-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n'],
        text=True, timeout=30)
    if len(native.encode()) > MAX_RECORD_BYTES:
        raise ValueError('compose_environment_native_inventory_too_large')
    packages = []
    for line in native.splitlines():
        name, version, architecture, status = line.split('\t')
        if status == 'installed':
            packages.append(dict(name=name, version=version, architecture=architecture))
    build = None
    if profile != 'proxy':
        file_identity(BUILD_RECORD, MAX_RECORD_BYTES)
        build = json.loads(BUILD_RECORD.read_text())
        if build['profile'] != profile:
            raise ValueError('compose_environment_build_profile_mismatch')
        verify_wheels(Path('/wheels'), build['wheels'])
    write_record(ENVIRONMENT_RECORD, dict(schema=1, profile=profile,
        base_image=spec['base_image'], debian_snapshot=spec['debian_snapshot'],
        python=platform.python_version(), architecture=platform.machine(),
        python_packages=actual, system_packages=packages, build=build,
        locks={p.name: file_identity(p, MAX_RECORD_BYTES) for p in sorted(LOCKS.iterdir())
            if p.is_file()}, bit_reproducible_build_claim=False))


if __name__ == '__main__':
    main()
