from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import redis
from atropos.storage_backup import (BackupError, admission_limits, capture, checked, read_backup,
                                    restore, summary, write_backup)
from atropos.test_execution import test_namespace
from atropos.redis_maintenance import RedisExecutionMaintenance

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from support import RedisFixture


class StorageBackupTests(unittest.TestCase):
    def setUp(self):
        self.source = RedisFixture(); self.addCleanup(self.source.close)
        self.destination = RedisFixture(); self.addCleanup(self.destination.close)
        self.raw = redis.Redis(host='127.0.0.1', port=self.source.port, decode_responses=False)
        self.target = redis.Redis(host='127.0.0.1', port=self.destination.port, decode_responses=False)
        self.addCleanup(self.raw.close); self.addCleanup(self.target.close)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.namespace = 'backup'
        RedisExecutionMaintenance(self.raw, self.namespace).pause()
        self.raw.set('backup:rules:demo:cpu:head', 'revision')
        self.raw.set('backup:run:uncertain', json.dumps({'status':'uncertain','id':'uncertain'}))
        self.lock_key = 'backup:target:' + hashlib.sha256(b'synthetic-target').hexdigest()
        self.raw.set(self.lock_key, 'uncertain')
        self.raw.sadd('backup:uncertain', 'uncertain')

    def test_restore_preserves_binary_data_uncertainty_and_paused_queues(self):
        self.raw.set('backup:binary', b'\xff\x00\xfe')
        payload = capture(self.raw, self.namespace)
        path = self.root/'backup.json'; write_backup(payload, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        result = restore(self.target, self.namespace, read_backup(path), source_stopped=True)
        self.assertEqual(result['execution'], 'paused')
        self.assertEqual(self.target.get('backup:binary'), b'\xff\x00\xfe')
        self.assertEqual(self.target.get(self.lock_key), b'uncertain')
        self.assertEqual(self.target.get(test_namespace('backup')+':execution-paused'), b'1')
        self.assertNotIn('entries', summary(payload))

    def test_missing_or_expiring_target_exclusion_is_rejected(self):
        self.raw.delete(self.lock_key)
        with self.assertRaisesRegex(BackupError, 'target exclusion'):
            capture(self.raw, self.namespace)
        self.raw.set(self.lock_key, 'uncertain', px=60000)
        with self.assertRaisesRegex(BackupError, 'target exclusion'):
            capture(self.raw, self.namespace)

    def test_uncertainty_index_and_lock_owner_must_match_retained_runs(self):
        self.raw.srem('backup:uncertain', 'uncertain')
        with self.assertRaisesRegex(BackupError, 'target exclusion'):
            capture(self.raw, self.namespace)
        self.raw.sadd('backup:uncertain', 'uncertain')
        self.raw.set(self.lock_key, 'missing')
        with self.assertRaisesRegex(BackupError, 'target exclusion'):
            capture(self.raw, self.namespace)
        self.raw.set(self.lock_key, 'uncertain')
        self.raw.sadd('backup:uncertain', *('orphan-%d' % index for index in range(20)))
        with self.assertRaisesRegex(BackupError, 'target exclusion'):
            capture(self.raw, self.namespace)

    def test_restore_without_exclusion_or_index_rolls_back_all_keys(self):
        original = capture(self.raw, self.namespace)
        for missing in (self.lock_key, 'backup:uncertain'):
            with self.subTest(missing=missing):
                payload = deepcopy(original)
                payload['entries'] = [entry for entry in payload['entries'] if entry['key'] != missing]
                with self.assertRaisesRegex(BackupError, 'in-flight or invalid'):
                    restore(self.target, self.namespace, payload, source_stopped=True)
                self.assertEqual(self.target.dbsize(), 0)

    def test_admission_pause_and_quiescence_are_checked_atomically(self):
        self.raw.delete('backup:execution-paused')
        with self.assertRaisesRegex(BackupError, 'Pause both'): capture(self.raw, self.namespace)
        RedisExecutionMaintenance(self.raw, self.namespace).pause()
        for status in ('running',):
            self.raw.set('backup:run:active', json.dumps({'status':status}))
            with self.assertRaisesRegex(BackupError, 'Wait for'): capture(self.raw, self.namespace)
        self.raw.delete('backup:run:active')
        self.raw.set('backup:lease:test','token',px=10000)
        with self.assertRaisesRegex(BackupError, 'Wait for'): capture(self.raw,self.namespace)
        self.raw.delete('backup:lease:test')
        self.raw.xadd('backup:queue', {'run_id':'queued'})
        with self.assertRaisesRegex(BackupError, 'Wait for'): capture(self.raw,self.namespace)

    def test_shared_source_and_nonempty_destination_are_never_modified(self):
        payload = capture(self.raw, self.namespace)
        self.raw.set('another-app:key','preserve')
        with self.assertRaisesRegex(BackupError, 'dedicated'): capture(self.raw,self.namespace)
        self.target.set('existing','preserve')
        with self.assertRaisesRegex(BackupError, 'empty destination'):
            restore(self.target,self.namespace,payload,source_stopped=True)
        self.assertEqual(self.target.keys(),[b'existing'])
        with self.assertRaises(ValueError): restore(self.target,self.namespace,payload)
        with self.assertRaises(ValueError): restore(self.target,'other',payload,source_stopped=True)

    def test_bad_dump_rolls_back_all_created_keys(self):
        payload = capture(self.raw,self.namespace)
        payload['entries'][-1]['dump'] = base64.b64encode(b'not-a-redis-dump').decode()
        with self.assertRaisesRegex(BackupError,'invalid storage dump'):
            restore(self.target,self.namespace,payload,source_stopped=True)
        self.assertEqual(self.target.dbsize(),0)

    def test_corruption_duplicate_keys_permissions_and_overwrite_are_rejected(self):
        payload=capture(self.raw,self.namespace);path=self.root/'backup.json'
        write_backup(payload,path);original=path.read_bytes()
        with self.assertRaises(FileExistsError):write_backup(payload,path)
        self.assertEqual(path.read_bytes(),original)
        path.chmod(0o644)
        with self.assertRaises(ValueError):read_backup(path)
        path.chmod(0o600)
        outer=json.loads(original);outer['payload']['namespace']='changed';path.write_text(json.dumps(outer))
        with self.assertRaisesRegex(ValueError,'integrity'):read_backup(path)
        bad=deepcopy(payload);bad['entries'].append(bad['entries'][0])
        with self.assertRaises(ValueError):checked(bad)
        bad=deepcopy(payload);bad['entries'][0]['key']='foreign:key'
        with self.assertRaises(ValueError):checked(bad)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(OSError):read_backup(link)

    def test_expired_ephemeral_keys_are_not_resurrected(self):
        self.raw.set('backup:proof:expired','value')
        payload=capture(self.raw,self.namespace)
        for entry in payload['entries']:
            if entry['key']=='backup:proof:expired':entry['expires_at_ms']=1
        restore(self.target,self.namespace,payload,source_stopped=True)
        self.assertIsNone(self.target.get('backup:proof:expired'))

    def test_forged_runnable_dump_is_rejected_and_rolled_back(self):
        payload=capture(self.raw,self.namespace)
        self.raw.set('backup:run:uncertain',json.dumps({'status':'queued'}))
        for entry in payload['entries']:
            if entry['key']=='backup:run:uncertain':
                entry['dump']=base64.b64encode(self.raw.dump(entry['key'])).decode()
        with self.assertRaisesRegex(BackupError,'in-flight or invalid'):
            restore(self.target,self.namespace,payload,source_stopped=True)
        self.assertEqual(self.target.dbsize(),0)
