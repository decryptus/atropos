"""WAL searches must not reserve writers or retain stale authorization."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

import test_sqlite_rules as fixtures
from atropos.authorization import AccessPolicy
from atropos.backend_registry import open_storage
from atropos.intervention_search import InterventionQuery, search
from atropos.sql_search import SQLInterventionIndex


class SQLiteReadSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SQLiteRuleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = self.fixture.backend
        self.spec = {'backend': 'sqlite', 'url': 'sqlite:///' + self.backend.path}

    def test_open_and_read_complete_while_another_writer_has_uncommitted_changes(self):
        self.backend.set('visible', 'committed')
        def read():
            backend = open_storage(self.spec)
            try: return backend.get('visible')
            finally: backend.close()
        with ThreadPoolExecutor(1) as pool:
            with self.backend.transaction():
                self.backend.set('visible', 'uncommitted')
                # The writer cannot finish until the read returns. A write
                # reservation during opening deterministically fails this gate.
                self.assertEqual(pool.submit(read).result(timeout=2), 'committed')
        self.assertEqual(self.backend.get('visible'), 'uncommitted')

    def test_search_snapshot_keeps_its_view_while_another_connection_commits(self):
        f = self.fixture
        f.submit()
        other = open_storage(self.spec)
        self.addCleanup(other.close)
        index = f.store.intervention_index(f.dispatcher.tests.workflows.store)
        with index.snapshot():
            before = self.backend.connection.execute('SELECT COUNT(*) FROM journal').fetchone()[0]
            other.append('synthetic-writer', '{}')
            self.assertEqual(self.backend.connection.execute('SELECT COUNT(*) FROM journal').fetchone()[0], before)
            scopes = [(f.draft['revision'], 'real', 'org/server/host')]
            total, items = index.page('alice', 'org', 'cpu', InterventionQuery(), scopes)
            self.assertEqual(total, 1)
            self.assertEqual(len(items), 1)
        self.assertEqual(self.backend.connection.execute('SELECT COUNT(*) FROM journal').fetchone()[0], before+1)

    def test_permissions_committed_during_snapshot_are_rechecked_after_it(self):
        f = self.fixture
        f.submit()
        self.backend.set('read-enabled', 'yes')
        policy = f.policy()
        f.lifecycle.policy_loader = lambda: policy if self.backend.get('read-enabled') else AccessPolicy([], [])
        self.assert_revocation(lambda backend: backend.delete('read-enabled'), 'rule_access_denied')

    def test_identity_committed_during_snapshot_is_rechecked_after_it(self):
        f = self.fixture
        f.submit()
        self.backend.set('credential', 'current')
        f.dispatcher.identity_loader = lambda actor: self.backend.get('credential')
        self.assert_revocation(lambda backend: backend.set('credential', 'revoked'), 'dispatch_identity_changed')

    def assert_revocation(self, revoke, error):
        other = open_storage(self.spec)
        self.addCleanup(other.close)
        original = SQLInterventionIndex.page
        def page(index, *args):
            result = original(index, *args)
            revoke(other)
            return result
        with patch.object(SQLInterventionIndex, 'page', page):
            with self.assertRaisesRegex(PermissionError, error):
                search(self.fixture.dispatcher, self.fixture.actor, 'cpu', InterventionQuery())

    def test_product_write_cannot_upgrade_a_read_snapshot(self):
        with self.backend.read_snapshot():
            self.assertIsNone(self.backend.get('forbidden'))
            with self.backend.read_snapshot():
                self.assertIsNone(self.backend.get('forbidden'))
            with self.assertRaisesRegex(ValueError, 'write_inside_read_snapshot'):
                self.backend.set('forbidden', 'value')
        self.assertIsNone(self.backend.get('forbidden'))
        with self.backend.transaction():
            with self.backend.read_snapshot():
                self.backend.set('allowed', 'value')
        self.assertEqual(self.backend.get('allowed'), 'value')

    def test_concurrent_first_open_rechecks_initialization_under_writer_lock(self):
        spec = {'backend': 'sqlite', 'url': 'sqlite:///' + self.fixture.directory.name + '/new.db'}
        ready = threading.Barrier(2)
        def initialize(_):
            ready.wait(timeout=2)
            backend = open_storage(spec)
            try:
                backend.set('initialized', 'yes')
                return backend.get('initialized')
            finally:
                backend.close()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(initialize, range(2))), ['yes', 'yes'])
