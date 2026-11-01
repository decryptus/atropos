"""Release only tagged runtime bytes; preserve immutable tags and newer aliases."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib
import urllib.error
import urllib.request

VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


def stable_version(text):
    match = VERSION.fullmatch(text)
    if not match:
        raise ValueError('stable_container_version_required')
    return tuple(map(int, match.groups()))


def aliases(version, published):
    current = stable_version(version)
    versions = [stable_version(name) for name in published if VERSION.fullmatch(name)]
    result = []
    for length, alias in [(2, '.'.join(version.split('.')[:2])), (1, version.split('.')[0]), (0, 'latest')]:
        if not any(other[:length] == current[:length] and other > current for other in versions):
            result.append(alias)
    return result


def git(*args):
    return subprocess.check_output(['git', *args], timeout=30)


def verify_release(root, version, package):
    stable_version(version)
    tag = 'v' + version
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    if metadata['version'] != version:
        raise ValueError('container_version_mismatch')
    if git('show', tag + ':pyproject.toml') != (root / 'pyproject.toml').read_bytes():
        raise ValueError('released_package_metadata_mismatch')
    prefixes = [package + '/'] + (['client/atropos_client/'] if package == 'atropos' else [])
    tagged = git('ls-tree', '-r', '--name-only', tag).decode().splitlines()
    expected = {p for p in tagged if p.endswith('.py') and any(p.startswith(prefix) for prefix in prefixes)}
    actual = {p.as_posix() for prefix in prefixes for p in Path(prefix).rglob('*.py')}
    if not expected or expected != actual:
        raise ValueError('released_runtime_file_set_mismatch')
    for path in sorted(expected):
        if git('show', tag + ':' + path) != (root / path).read_bytes():
            raise ValueError('released_runtime_bytes_mismatch')
    if package == 'atropos' and git('show', tag + ':client/pyproject.toml') != (root / 'client/pyproject.toml').read_bytes():
        raise ValueError('released_client_metadata_mismatch')


def request(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'atraxis-container-release'}), timeout=30) as response:
        return json.load(response)


def published_tags(repository):
    # Confirm the repository exists before interpreting an absent tag.
    base = 'https://hub.docker.com/v2/repositories/' + repository + '/'
    request(base)
    url = base + 'tags?page_size=100'
    names = []
    for _ in range(50):
        if not url.startswith(base):
            raise ValueError('unexpected_registry_pagination')
        result = request(url)
        names.extend(item['name'] for item in result['results'])
        url = result.get('next')
        if not url:
            return names
    raise ValueError('registry_tag_limit')


def publish(root, package, image):
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    verify_release(root, version, package)
    repository = 'decryptus/' + package
    names = published_tags(repository)
    exact = repository + ':' + version
    if version in names:
        subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', exact], check=True, timeout=300)
        def identity(name):
            return subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', name], timeout=30).strip()
        if identity(exact) != identity(image):
            raise ValueError('existing_version_tag_is_immutable')
    for tag in ([version] if version not in names else []) + aliases(version, names):
        destination = repository + ':' + tag
        subprocess.run(['docker', 'tag', image, destination], check=True, timeout=30)
        subprocess.run(['docker', 'push', destination], check=True, timeout=300)
    print('Published tested image:', exact)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', choices=['atraxis', 'atropos'])
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    publish(Path.cwd(), args.package, args.image)
