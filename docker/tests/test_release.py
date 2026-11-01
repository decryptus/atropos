import importlib.util
from pathlib import Path
import subprocess
import os
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('container_release', Path(__file__).resolve().parents[1] / 'release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_only_complete_stable_versions_can_publish(self):
        self.assertEqual(release.stable_version('1.0.1'), (1, 0, 1))
        for version in ['1.0.1.dev0', '1.0.1rc1', 'v1.0.1', '01.0.1', '1.0', '1.0.1\n']:
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.stable_version(version)

    def test_aliases_never_downgrade_a_newer_release(self):
        self.assertEqual(release.aliases('1.0.1', []), ['1.0', '1', 'latest'])
        self.assertEqual(release.aliases('1.0.1', ['1.0.2']), [])
        self.assertEqual(release.aliases('1.0.1', ['1.1.0', 'latest']), ['1.0'])
        self.assertEqual(release.aliases('1.0.1', ['2.0.0']), ['1.0', '1'])

    def test_release_rejects_changed_or_added_runtime_and_metadata(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chdir(root)
            try:
                def git(*args):
                    subprocess.run(['git', *args], check=True, capture_output=True)
                git('init'); git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture')
                (root / 'atraxis').mkdir()
                source = root / 'atraxis/__init__.py'; source.write_text('# original\n')
                metadata = root / 'pyproject.toml'; metadata.write_text('[project]\nname="atraxis"\nversion="1.0.0"\n')
                git('add', '.'); git('commit', '-m', 'fixture'); git('tag', 'v1.0.0')
                release.verify_release(root, '1.0.0', 'atraxis')
                source.write_text('# changed\n')
                with self.assertRaisesRegex(ValueError, 'runtime_bytes'):
                    release.verify_release(root, '1.0.0', 'atraxis')
                source.write_text('# original\n')
                added = root / 'atraxis/new.py'; added.write_text('# new\n')
                with self.assertRaisesRegex(ValueError, 'file_set'):
                    release.verify_release(root, '1.0.0', 'atraxis')
                added.unlink(); metadata.write_text(metadata.read_text()+'dependencies=["unexpected"]\n')
                with self.assertRaisesRegex(ValueError, 'metadata'):
                    release.verify_release(root, '1.0.0', 'atraxis')
            finally:
                os.chdir(previous)
