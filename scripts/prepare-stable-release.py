"""Stage the exact qualified engine/client archives for the bounded 1.0 release."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tomllib


def prepare(root: Path) -> None:
    projects = [tomllib.loads((root / p).read_text())['project']
                for p in ('pyproject.toml', 'client/pyproject.toml')]
    packages = {p['name']: p['version'] for p in projects}
    expected_packages = {'atraxis-atropos': '1.0.0', 'atropos-client': '1.0.0'}
    if packages != expected_packages:
        raise ValueError('stable_package_versions_required')
    if 'atropos-client[observations]==1.0.0' not in projects[0]['optional-dependencies']['service']:
        raise ValueError('matching_service_client_required')
    qualification = json.loads((root / 'docs/releases/1.0.0-qualification.json').read_text())
    if (qualification.get('schema') != 1 or qualification.get('version') != '1.0.0'
            or qualification.get('packages') != packages
            or qualification.get('gates') != {'G1': 'closed', 'G2': 'closed', 'G3': 'closed'}
            or qualification.get('support_matrix') != 'docs/user/v1.0.md'
            or set(qualification.get('evidence_sha256', {})) != {'cold', 'reference', 'endurance'}
            or any(not re.fullmatch('[0-9a-f]{64}', value)
                   for value in qualification['evidence_sha256'].values())):
        raise ValueError('reviewed_operational_gates_required')
    paths = sorted(p for name in ('atropos', 'client/atropos_client')
                   for p in (root / name).rglob('*.py'))
    if any(p.is_symlink() for p in paths):
        raise ValueError('runtime_identity_mismatch')
    runtime = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    if not runtime or runtime != qualification.get('runtime_files_sha256'):
        raise ValueError('runtime_identity_mismatch')
    evidence = json.loads((root / 'stable-ci.json').read_text())
    download = root / 'stable-download'
    manifest = json.loads((download / 'package-evidence/manifest.json').read_text())
    if (manifest.get('source_commit') != evidence['tested_commit']
            or manifest.get('source_tree') != evidence['source_tree']
            or manifest.get('packages') != packages):
        raise ValueError('package_identity_mismatch')
    names = {'dist/atraxis_atropos-1.0.0-py3-none-any.whl', 'dist/atraxis_atropos-1.0.0.tar.gz',
             'client/dist/atropos_client-1.0.0-py3-none-any.whl', 'client/dist/atropos_client-1.0.0.tar.gz'}
    actual = {str(p.relative_to(download)) for d in ('dist', 'client/dist')
              for p in (download / d).iterdir()}
    if set(manifest['files']) != names or actual != names:
        raise ValueError('unexpected_archive_files')
    for name in names:
        path = download / name
        entry = manifest['files'][name]
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError('invalid_archive')
        if path.stat().st_size != entry['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('archive_digest_mismatch')
    assets = root / 'stable-assets'
    assets.mkdir(exist_ok=False)
    for name in sorted(names):
        shutil.copyfile(download / name, assets / Path(name).name)
    evidence.update(version='1.0.0', packages=packages, package_manifest=manifest,
                    operational_qualification=qualification, operational_gates_closed=True,
                    prerelease=False, registry_publication=False)
    (assets / 'release-provenance.json').write_text(json.dumps(evidence, indent=2) + '\n')
    checksums = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in sorted(assets.iterdir())]
    (assets / 'SHA256SUMS').write_text(''.join(checksums))
    print('Verified exact engine/client archives and bounded operational qualification: 1.0.0')


if __name__ == '__main__':
    prepare(Path.cwd())
