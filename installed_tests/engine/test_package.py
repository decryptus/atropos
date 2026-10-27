"""Run with only the built engine wheel and its base dependencies installed."""
import importlib.metadata
import importlib.util
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ENGINE_DISTRIBUTION, InstalledCase, verify_package


class EnginePackageTests(InstalledCase):
    def test_installed_bytes_version_license_and_independent_base_cli(self):
        verify_package(self, 'atropos', ENGINE_DISTRIBUTION, 'atropos')
        for module in ('atropos_client', 'redis', 'zen', 'auton'):
            self.assertIsNone(importlib.util.find_spec(module))
        self.assertIn('sonicprobe==0.3.62', importlib.metadata.requires(ENGINE_DISTRIBUTION))
        self.assertIn('storage-backup', self.cli('--help').stdout)
        self.cli(code=2)
        # Configured rule-service operations require the documented optional extra.
        result = self.cli('storage-status', '--config', self.profile('engine.db'), code=2)
        self.assertIn('Optional dependencies required', result.stderr)
        self.assertFalse((self.root / 'engine.db').exists())

    def test_installed_workflow_requires_explicit_worker_and_preserves_deduplication(self):
        workflow = self.workflow()
        self.assertEqual(self.value('validate', workflow), {'valid': True})
        self.assertEqual(self.value('plan', workflow)['steps'], ['echo'])
        self.assertFalse((self.root / 'engine.db').exists())
        args = ('submit', workflow, '--target', 'fixture', '--dedup-key', 'one')
        submitted = self.value(*args)
        self.assertEqual(submitted['status'], 'queued')
        self.assertEqual(self.value(*args), submitted)
        self.cli('worker', '--once')
        completed = self.value('status', submitted['id'])
        self.assertEqual(completed['status'], 'succeeded')
        self.assertEqual(completed['results'], {'echo': {'message': 'installed-wheel'}})
        self.assertEqual(self.value(*args)['id'], submitted['id'])
        self.cli('worker', '--once')
        self.assertEqual(self.value('status', submitted['id']), completed)
        self.assertEqual((self.root / 'engine.db').stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.root / 'engine.db') as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unsupported_database_is_refused_without_rewriting_existing_data(self):
        path = self.root / 'engine.db'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE sentinel(value TEXT)')
            connection.execute("INSERT INTO sentinel VALUES('preserve')")
        path.chmod(0o600)
        original = path.read_bytes()
        self.cli('queue-status', code=2)
        self.assertEqual(path.read_bytes(), original)
