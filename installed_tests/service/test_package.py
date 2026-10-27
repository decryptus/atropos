"""Run against built engine[service] and client[observations] wheels only."""
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (ACTOR, CLIENT_DISTRIBUTION, ENGINE_DISTRIBUTION, InstalledCase,
    LOOPBACK_HOST, LOOPBACK_ORIGIN, TOKEN, verify_package)
from atropos_client.client import RuleClient, RemoteRuleError

STARTUP_SECONDS = 15


class ServicePackageTests(InstalledCase):
    def setUp(self):
        super().setUp()
        verify_package(self, 'atropos', ENGINE_DISTRIBUTION, 'atropos')
        verify_package(self, 'atropos_client', CLIENT_DISTRIBUTION, 'client/atropos_client')

    def test_installed_rule_service_authentication_and_actor_refusal(self):
        profile = self.profile('engine.db')
        with socket.socket() as reservation:
            reservation.bind((LOOPBACK_HOST, 0))
            port = reservation.getsockname()[1]
        self.private('token', TOKEN)
        client_profile = self.private('client.json', json.dumps(dict(origin=LOOPBACK_ORIGIN.format(port=port),
            token_file='token', allow_loopback_http=True, timeout=2)))
        client = RuleClient(client_profile, expected_actor=ACTOR)
        with (self.root / 'service.log').open('w+') as log:
            process = subprocess.Popen(self.command + ['rules-serve', '--config', str(profile), '--port', str(port)],
                cwd=self.root, env=self.environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + STARTUP_SECONDS
                while True:
                    self.assertIsNone(process.poll(), 'Installed rule service stopped before readiness')
                    try:
                        self.assertTrue(client.catalog())
                        break
                    except RemoteRuleError:
                        self.assertLess(time.monotonic(), deadline, 'Installed rule service startup timed out')
                        time.sleep(0.05)
                wrong_actor = RuleClient(client_profile, expected_actor=dict(ACTOR, subject='other'))
                try:
                    with self.assertRaises(PermissionError):
                        wrong_actor.catalog()
                finally:
                    wrong_actor.close()
                self.private('token', 'b' * 43)
                with self.assertRaises(PermissionError):
                    client.catalog()
                with sqlite3.connect(self.root / 'engine.db') as connection:
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
            finally:
                client.close()
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    self.fail('Installed rule service did not terminate')
            self.assertEqual(process.returncode, 0)

    def test_installed_backup_restore_keeps_queue_pause_and_requires_explicit_resume(self):
        workflow = self.workflow()
        source, target = self.profile('engine.db'), self.profile('restored.db')
        self.cli('queue-configure', '--max-outstanding', '7')
        args = ('submit', workflow, '--target', 'fixture', '--dedup-key', 'one')
        submitted = self.value(*args)
        before = self.value('status', submitted['id'])
        archive = self.root / 'backup.json'
        self.cli('storage-backup', '--config', source, '--output', archive)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        original = archive.read_bytes()
        self.cli('storage-backup-inspect', '--input', archive)
        self.cli('storage-backup', '--config', source, '--output', archive, code=2)
        self.assertEqual(archive.read_bytes(), original)
        restore = ('storage-restore', '--config', target, '--input', archive)
        self.cli(*restore, code=2)
        self.assertFalse((self.root / 'restored.db').exists())
        self.cli(*restore, '--confirm-source-stopped')
        status = self.value('storage-status', '--config', target)
        self.assertTrue(status['execution_paused'])
        self.assertTrue(status['test_execution_paused'])
        self.assertEqual(self.value('queue-status', database='restored.db')['max_outstanding'], 7)
        self.assertEqual(self.value('status', submitted['id'], database='restored.db'), before)
        self.cli('worker', '--once', database='restored.db')
        self.assertEqual(self.value('status', submitted['id'], database='restored.db'), before)
        self.cli('storage-resume', '--config', target, code=2)
        self.cli('storage-resume', '--config', target, '--confirm-reconciled')
        self.cli('worker', '--once', database='restored.db')
        completed = self.value('status', submitted['id'], database='restored.db')
        self.assertEqual(completed['status'], 'succeeded')
        self.assertEqual(completed['results'], {'echo': {'message': 'installed-wheel'}})
        self.assertEqual(self.value(*args, database='restored.db')['id'], submitted['id'])
        self.cli(*restore, '--confirm-source-stopped', code=2)
        self.assertEqual(self.value('status', submitted['id'], database='restored.db'), completed)
        self.assertEqual(self.value('status', submitted['id']), before)
        self.assertEqual(archive.read_bytes(), original)
        with sqlite3.connect(self.root / 'restored.db') as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
