"""Promote the exact 1.0.0 GitHub assets; never rebuild or overwrite a file."""
import argparse
import email.parser
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MANIFEST = Path(__file__).with_name('pypi-1.0.0.json')
LIMIT = 10 * 1024 * 1024


def digest(path):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= LIMIT:
        raise ValueError('invalid_asset')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archives(manifest, role):
    prefix = manifest['packages'][role].replace('-', '_') + '-' + manifest['version']
    return {name: sha for name, sha in manifest['files'].items()
            if name.startswith(prefix) and name.endswith(('.whl', '.tar.gz'))}


def validate(source, manifest, release, tag):
    if (release.get('tag_name') != manifest['tag'] or release.get('draft') is not False
            or release.get('prerelease') is not False
            or tag.get('object') != {'type': 'commit', 'sha': manifest['release_commit'],
                'url': 'https://api.github.com/repos/decryptus/atropos/git/commits/' + manifest['release_commit']}):
        raise ValueError('release_identity_mismatch')
    if source.is_symlink() or set(p.name for p in source.iterdir()) != set(manifest['files']):
        raise ValueError('unexpected_assets')
    for name, expected in manifest['files'].items():
        if Path(name).name != name or digest(source / name) != expected:
            raise ValueError('asset_digest_mismatch')
    provenance = json.loads((source / 'release-provenance.json').read_bytes())
    if (provenance['release_commit'] != manifest['release_commit'] or provenance['version'] != manifest['version']
            or provenance['prerelease'] or not provenance['operational_gates_closed']):
        raise ValueError('unqualified_release')
    for role, project in manifest['packages'].items():
        selected = archives(manifest, role)
        if len(selected) != 2: raise ValueError('wheel_and_source_required')
        for name in selected:
            if name.endswith('.whl'):
                with zipfile.ZipFile(source / name) as archive:
                    candidates = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
                    if len(candidates) != 1 or archive.getinfo(candidates[0]).file_size > LIMIT:
                        raise ValueError('invalid_metadata')
                    raw = archive.read(candidates[0])
            else:
                with tarfile.open(source / name) as archive:
                    candidates = [m for m in archive.getmembers() if m.name.count('/') == 1 and m.name.endswith('/PKG-INFO')]
                    if len(candidates) != 1 or not candidates[0].isfile() or candidates[0].size > LIMIT:
                        raise ValueError('invalid_metadata')
                    raw = archive.extractfile(candidates[0]).read(LIMIT + 1)
            metadata = email.parser.BytesParser().parsebytes(raw)
            if metadata['Name'] != project or metadata['Version'] != manifest['version']:
                raise ValueError('package_identity_mismatch')


def registry(project, version):
    url = 'https://pypi.org/pypi/' + project + '/' + version + '/json'
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT: raise ValueError('oversized_registry_response')
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code == 404: return None
        raise


def missing_files(expected, document, project, version):
    if document is None: return sorted(expected)
    info = document.get('info', {})
    if info.get('name') != project or info.get('version') != version:
        raise ValueError('registry_identity_mismatch')
    found = {}
    for item in document.get('urls', []):
        name = item['filename']
        if name in found or name not in expected or item.get('yanked') or item['digests']['sha256'] != expected[name]:
            raise ValueError('existing_registry_file_mismatch')
        found[name] = item
    return sorted(set(expected) - set(found))


def stage(source, destination, manifest, documents):
    # Resolve both projects before creating anything, including partial resumes.
    missing = {role: missing_files(archives(manifest, role), documents[role], project, manifest['version'])
               for role, project in manifest['packages'].items()}
    destination.mkdir(exist_ok=False)
    for role, names in missing.items():
        target = destination / role; target.mkdir()
        for name in names:
            if digest(source / name) != manifest['files'][name]: raise ValueError('asset_digest_mismatch')
            shutil.copyfile(source / name, target / name)
    return missing


def verify_registry(manifest):
    for role, project in manifest['packages'].items():
        expected = archives(manifest, role)
        document = registry(project, manifest['version'])
        if missing_files(expected, document, project, manifest['version']):
            raise ValueError('registry_release_incomplete')
        for item in document['urls']:
            url = urllib.parse.urlsplit(item['url'])
            if url.scheme != 'https' or url.hostname != 'files.pythonhosted.org':
                raise ValueError('unexpected_distribution_host')
            with urllib.request.urlopen(item['url'], timeout=30) as response:
                data = response.read(LIMIT + 1)
            if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != expected[item['filename']]:
                raise ValueError('published_bytes_mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-registry', action='store_true')
    parser.add_argument('--source', type=Path, default=Path('pypi-source'))
    parser.add_argument('--destination', type=Path, default=Path('pypi-upload'))
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_bytes())
    if args.verify_registry:
        verify_registry(manifest)
        print('Both PyPI releases and all four downloaded archives match the approved SHA-256 values')
        return
    validate(args.source, manifest, json.loads(Path('pypi-release.json').read_bytes()),
             json.loads(Path('pypi-tag.json').read_bytes()))
    documents = {role: registry(project, manifest['version']) for role, project in manifest['packages'].items()}
    missing = stage(args.source, args.destination, manifest, documents)
    result = dict(version=manifest['version'], source='GitHub release v1.0.0', rebuilt=False,
                  pending_uploads=missing, registry_complete=not any(missing.values()))
    Path('pypi-plan.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
