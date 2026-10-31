import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stable_release', SOURCE / 'scripts/prepare-stable-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class StableReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('pyproject.toml', 'client/pyproject.toml'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((SOURCE / name).read_bytes())
        self.modules = {}
        for name in ('atropos/__init__.py', 'client/atropos_client/__init__.py'):
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('__version__ = "1.0.0"\n')
            self.modules[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        self.qualification = dict(schema=1, version='1.0.0', packages={'atraxis-atropos':'1.0.0','atropos-client':'1.0.0'},
            gates={'G1':'closed','G2':'closed','G3':'closed'}, support_matrix='docs/user/v1.0.md',
            evidence_sha256={name:'a'*64 for name in ('cold','reference','endurance')},
            runtime_files_sha256=self.modules)
        self.save('docs/releases/1.0.0-qualification.json', self.qualification)
        self.save('stable-ci.json', dict(tested_commit='a'*40, source_tree='b'*40))
        self.manifest = dict(source_commit='a'*40, source_tree='b'*40, packages=self.qualification['packages'], files={})
        for name in ('dist/atraxis_atropos-1.0.0-py3-none-any.whl','dist/atraxis_atropos-1.0.0.tar.gz',
                     'client/dist/atropos_client-1.0.0-py3-none-any.whl','client/dist/atropos_client-1.0.0.tar.gz'):
            p = self.root / 'stable-download' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(('qualified '+name).encode())
            self.manifest['files'][name] = dict(size=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        self.save('stable-download/package-evidence/manifest.json', self.manifest)

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_stages_only_exact_tested_archives_with_provenance_and_checksums(self):
        release.prepare(self.root)
        assets = self.root / 'stable-assets'
        self.assertEqual(len(list(assets.iterdir())), 6)
        for name in self.manifest['files']:
            self.assertEqual((assets / Path(name).name).read_bytes(), (self.root / 'stable-download' / name).read_bytes())
        evidence = json.loads((assets / 'release-provenance.json').read_text())
        self.assertFalse(evidence['prerelease'])
        self.assertFalse(evidence['registry_publication'])
        for line in (assets / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split()
            self.assertEqual(digest, hashlib.sha256((assets / name).read_bytes()).hexdigest())

    def test_rejects_nonfinal_package(self):
        p = self.root / 'client/pyproject.toml'
        p.write_text(p.read_text().replace('version = "1.0.0"', 'version = "1.0.0rc1"'))
        with self.assertRaisesRegex(ValueError, 'stable_package_versions'):
            release.prepare(self.root)

    def test_rejects_a_service_pinned_to_the_old_client(self):
        p = self.root / 'pyproject.toml'
        p.write_text(p.read_text().replace('observations]==1.0.0','observations]==0.1.0a10'))
        with self.assertRaisesRegex(ValueError, 'matching_service_client'):
            release.prepare(self.root)

    def test_rejects_open_or_unbound_operational_gates(self):
        for key, value in [('gates', {'G1':'closed','G2':'open','G3':'closed'}), ('evidence_sha256', {})]:
            self.save('docs/releases/1.0.0-qualification.json', dict(self.qualification, **{key:value}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'operational_gates'):
                release.prepare(self.root)

    def test_rejects_changed_extra_and_symlinked_runtime(self):
        p = self.root / 'atropos/extra.py'
        for operation in ('extra', 'symlink'):
            if operation == 'extra': p.write_text('unaudited = True\n')
            else: p.symlink_to(self.root / 'atropos/__init__.py')
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'runtime_identity'):
                release.prepare(self.root)
            p.unlink()
        (self.root / 'atropos/__init__.py').write_text('changed = True\n')
        with self.assertRaisesRegex(ValueError, 'runtime_identity'):
            release.prepare(self.root)

    def test_rejects_different_package_source_tree_or_versions(self):
        for key, value in [('source_commit','c'*40),('source_tree','d'*40),('packages',{})]:
            self.save('stable-download/package-evidence/manifest.json', dict(self.manifest, **{key:value}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'package_identity'):
                release.prepare(self.root)

    def test_rejects_changed_missing_extra_and_symlinked_archives(self):
        p = self.root / 'stable-download' / next(iter(self.manifest['files']))
        original = p.read_bytes()
        p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'archive_digest'):
            release.prepare(self.root)
        p.unlink()
        with self.assertRaisesRegex(ValueError, 'unexpected_archive'):
            release.prepare(self.root)
        p.symlink_to(self.root / 'pyproject.toml')
        with self.assertRaisesRegex(ValueError, 'invalid_archive'):
            release.prepare(self.root)
        p.unlink(); p.write_bytes(original)
        p.with_name('extra.whl').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'unexpected_archive'):
            release.prepare(self.root)

    def test_existing_staged_release_is_never_overwritten(self):
        release.prepare(self.root)
        with self.assertRaises(FileExistsError):
            release.prepare(self.root)
