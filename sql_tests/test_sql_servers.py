"""Run the same durability and authorization contracts on actual SQL servers."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch

from support_sql import ServerFixture
import test_sqlite_backend as sqlite_workflows
import test_sqlite_rules as sqlite_rules
import test_reconciliation as reconciliation_tests
from support import workflow
from atropos.actions import builtin_actions
from atropos.backend_registry import open_storage
from atropos.definitions import encode
from atropos.intervention_search import search, InterventionQuery
from atropos.service import WorkflowService
from atropos.storage_backup import capture, restore, BackupError
from atropos.worker import Worker


class SQLWorkflowTests(ServerFixture):
    def setUp(self):
        super().setUp()
        self.store = self.backend.workflow_store('engine')
        self.actions = builtin_actions()
        self.service = WorkflowService(self.store,self.actions)

    submit = sqlite_workflows.SQLiteBackendTests.submit
    step = sqlite_workflows.SQLiteBackendTests.step

    def test_reopen_deduplication_results_and_history(self):
        sqlite_workflows.SQLiteBackendTests.test_reopen_retains_definitions_results_history_and_dedup(self)

    def test_concurrent_admission_and_capacity(self):
        sqlite_workflows.SQLiteBackendTests.test_two_connections_admit_once_and_capacity_is_atomic(self)

    def test_claim_race_fencing_and_target_exclusion(self):
        sqlite_workflows.SQLiteBackendTests.test_claim_race_fencing_and_uncertainty_preserve_target_exclusion(self)

    def test_atomic_conditions_audit_and_queue(self):
        sqlite_workflows.SQLiteBackendTests.test_condition_consumption_audit_and_queue_commit_or_rollback_together(self)

    def test_admission_rollback(self):
        sqlite_workflows.SQLiteBackendTests.test_rollback_after_state_write_preserves_evidence(self)

    def test_pause_and_test_namespace(self):
        sqlite_workflows.SQLiteBackendTests.test_pause_and_test_namespace_are_independent_of_admission(self)

    def test_redis_delivery_loss_and_ack_uncertainty(self):
        sqlite_workflows.SQLiteBackendTests.test_sqlite_with_redis_delivery_recovers_lost_broker_and_ack_failure(self)

    def test_delayed_delivery(self):
        sqlite_workflows.SQLiteBackendTests.test_delayed_step_is_not_dispatched_early(self)

    def test_paused_recovery_prioritizes_expired_leases(self):
        sqlite_workflows.SQLiteBackendTests.test_paused_recovery_prioritizes_expired_leases_over_full_ready_batch(self)

    def test_schema_constraints_reject_corruption(self):
        run = self.submit()
        cases = [
            ('INSERT INTO journal(id,"key",value) VALUES(-1,?,?)',('audit','{}')),
            ('INSERT INTO journal(id,"key",value) VALUES(0,?,?)',('audit','{}')),
            ('INSERT INTO records("key",value) VALUES(NULL,?)',('{}',)),
            ('UPDATE runs SET delivery=? WHERE id=?',('a'*31,run['id'])),
            ('UPDATE runs SET delivery=? WHERE id=?',('a'*33,run['id'])),
            ('UPDATE runs SET delivery=? WHERE id=?',('A'*32,run['id'])),
            ('UPDATE runs SET available=-1 WHERE id=?',(run['id'],)),
            ('UPDATE runs SET created=-1 WHERE id=?',(run['id'],)),
            ('UPDATE runs SET status=? WHERE id=?',('invalid',run['id'])),
            ('UPDATE runs SET state=? WHERE id=?',('{}',run['id'])),
            ('UPDATE runs SET state=? WHERE id=?',('broken',run['id'])),
            ('UPDATE runs SET state=? WHERE id=?',(encode(dict(run,created_at=str(run['created_at']))),run['id'])),
            ('UPDATE runs SET token=? WHERE id=?',('a'*32,run['id'])),
        ]
        for query,args in cases:
            with self.subTest(query=query,args=args),self.assertRaises(self.backend.database_errors):
                with self.backend.transaction(): self.backend.connection.execute(query,args)
        self.assertEqual(self.store.get(run['id']),run)

    def test_identity_cannot_be_rewritten(self):
        run=self.submit(attribution={'subject':'alice','organization':'org','issuer':'manual','workflow_revision':'synthetic'})
        changed=deepcopy(run);changed['attribution']['subject']='bob'
        with self.assertRaises(self.backend.database_errors):
            with self.backend.transaction():self.backend.connection.execute('UPDATE runs SET state=? WHERE id=?',(encode(changed),run['id']))
        self.assertEqual(self.store.get(run['id']),run)

    def test_unknown_schema_is_rejected_without_changes(self):
        run=self.submit()
        self.backend.connection.execute('UPDATE storage_metadata SET version=99')
        with self.assertRaisesRegex(ValueError,'unsupported_storage_format'):open_storage(self.spec)
        self.assertEqual(self.store.get(run['id']),run)

    def test_partial_initialization_is_not_resumed_or_deleted(self):
        spec=self.database_spec()
        from atropos.sql_server import SQLConnection, server_uri
        owner=self.admin_owner.__class__(server_uri(spec),auto_reconnect=False)
        connection=SQLConnection(owner)
        try:
            connection.execute('CREATE TABLE sentinel (id INTEGER PRIMARY KEY)')
            owner.commit()
            with self.assertRaisesRegex(ValueError,'unsupported_storage_format'):open_storage(spec)
            connection.execute('INSERT INTO sentinel VALUES(42)');owner.commit()
            self.assertEqual(connection.execute('SELECT id FROM sentinel').fetchone(),(42,))
        finally:connection.close()

    def test_commit_failure_is_not_replayed_or_reconnected(self):
        owner=self.backend._connection_owner
        real_execute=self.backend.connection.execute
        def uncertain_commit(statement, parameters=None):
            result=real_execute(statement,parameters)
            if statement=='COMMIT': raise RuntimeError('synthetic lost commit acknowledgement')
            return result
        with patch.object(self.backend.connection,'execute',side_effect=uncertain_commit),patch.object(owner,'reconnect',side_effect=AssertionError('replayed')),self.assertRaises(RuntimeError):
            self.submit(key='uncertain-commit')
        other=open_storage(self.spec);self.addCleanup(other.close)
        store=other.workflow_store('engine')
        self.assertEqual(store.queue_status()['outstanding'],1)
        self.assertEqual(WorkflowService(store,self.actions).submit(workflow(),{},'host','uncertain-commit')['status'],'queued')

    def test_backup_restore_preserves_paused_outbox_expiry_and_sequence(self):
        run=self.submit(key='backup')
        self.backend.set('engine:proof','expired',ex=-1)
        self.backend.append('engine:audit','first')
        with self.backend.transaction():self.backend.restore_sequence(42)
        self.backend.maintenance('engine').pause()
        payload=capture(self.backend,'engine')
        self.assertEqual(payload['journal_sequence'],42)
        from pathlib import Path
        import tempfile
        from atropos.storage_backup import write_backup
        from atropos.retention import inspect_archive
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'archive.json';write_backup(payload,archive)
            report=inspect_archive(archive)
        self.assertEqual(report['backend'],self.spec['backend'])
        self.assertEqual(report['real']['statuses']['queued'],1)
        self.assertFalse(report['purge_supported'])
        target=self.other_backend()
        restore(target,'engine',payload,source_stopped=True)
        restored=target.workflow_store('engine')
        self.assertEqual(restored.get(run['id']),run)
        self.assertIsNone(target.get('engine:proof'))
        self.assertEqual(target.append('engine:audit','next'),43)
        self.assertFalse(asyncio.run(Worker(restored,self.actions).run_once(0)))
        target.maintenance('engine').resume(reconciled=True)
        self.assertTrue(asyncio.run(Worker(restored,self.actions).run_once(0)))
        self.assertEqual(restored.get(run['id'])['status'],'succeeded')

    def test_lost_connection_rolls_back_without_reconnecting(self):
        self.backend.set('engine:proof','keep')
        pg=self.spec['backend']=='postgresql'
        connection_id=self.backend.connection.execute('SELECT pg_backend_pid()' if pg else 'SELECT CONNECTION_ID()').fetchone()[0]
        with self.assertRaises(self.backend.database_errors):
            with self.backend.transaction():
                self.backend.delete('engine:proof')
                self.submit(key='lost-connection')
                if pg:self.admin.execute('SELECT pg_terminate_backend(?)',(connection_id,)).close()
                else:self.admin.execute('KILL CONNECTION '+str(int(connection_id)))
        other=open_storage(self.spec);self.addCleanup(other.close)
        self.assertEqual(other.get('engine:proof'),'keep')
        self.assertEqual(other.workflow_store('engine').queue_status()['outstanding'],0)
        with self.assertRaises(self.backend.database_errors):self.backend.get('engine:proof')

    def test_bad_restore_rolls_back_and_preserves_existing_destination(self):
        self.submit();self.backend.maintenance('engine').pause()
        payload=capture(self.backend,'engine');bad=deepcopy(payload)
        bad['tables']['runs'][0][1]='short'
        target=self.other_backend()
        with self.assertRaises(BackupError):restore(target,'engine',bad,source_stopped=True)
        self.assertEqual(target.connection.execute('SELECT COUNT(*) FROM records').fetchone()[0],0)
        target.set('engine:sentinel','keep')
        with self.assertRaises(BackupError):restore(target,'engine',payload,source_stopped=True)
        self.assertEqual(target.get('engine:sentinel'),'keep')

    def test_backup_refuses_active_lease_and_keeps_uncertain_lock(self):
        run=self.submit()
        message,_=self.store.receive('lost',block_ms=0)
        self.store.claim(message[0],run['id'],'lost')
        self.backend.maintenance('engine').pause()
        with self.assertRaises(BackupError):capture(self.backend,'engine')
        self.backend.connection.execute('UPDATE runs SET lease_until=0')
        self.step()
        target=self.other_backend()
        restore(target,'engine',capture(self.backend,'engine'),source_stopped=True)
        self.assertEqual(target.workflow_store('engine').get(run['id'])['status'],'uncertain')
        self.assertEqual(target.get(self.store.target_key(run['target'])),run['id'])


class SQLRuleTests(ServerFixture):
    def setUp(self):
        super().setUp()
        sqlite_rules.SQLiteRuleTests.configure_application(self)

    submit=sqlite_rules.SQLiteRuleTests.submit

    def test_reconciliation_preserves_evidence_without_replaying(self):
        reconciliation_tests.exercise_resolution(self)

    def test_reconciliation_owner_and_current_permissions(self):
        reconciliation_tests.exercise_permissions(self)

    def test_reconciliation_stale_review_and_target_ownership(self):
        reconciliation_tests.exercise_conflicts(self)

    def test_reconciliation_audit_failure_rolls_back(self):
        reconciliation_tests.SQLiteReconciliationTests.test_audit_failure_rolls_back_state_and_target_release(self)

    def test_reconciliation_policy_and_state_races(self):
        reconciliation_tests.exercise_races(self)

    def test_guarded_dispatch_search_and_worker(self):
        sqlite_rules.SQLiteRuleTests.test_guarded_dispatch_search_and_results_survive_reopen(self)

    def test_search_authorization_revocation_and_test_separation(self):
        sqlite_rules.SQLiteRuleTests.test_search_is_owner_scoped_revocable_and_separates_test_work(self)

    def test_generation_cas_retirement_and_linked_evidence(self):
        sqlite_rules.SQLiteRuleTests.test_atomic_generation_checks_and_linked_evidence_prevent_delete(self)

    def test_draft_deletion_and_revision_cas(self):
        sqlite_rules.SQLiteRuleTests.test_draft_delete_preserves_audit_and_revision_cas(self)

    def test_indexed_search_without_payload_or_audit_scans(self):
        for i in range(4):self.submit('event-'+str(i))
        with patch.object(self.store,'get',side_effect=AssertionError('payload read')),patch.object(self.lifecycle.store,'admission_history',side_effect=AssertionError('audit scan')):
            result=search(self.dispatcher,self.actor,'cpu',InterventionQuery(query='EVENT-3',limit=1))
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['event_id'],'event-3')
        query='SELECT run_id FROM admissions WHERE organization=? AND subject=? AND rule=? AND namespace=?'
        if self.spec['backend']=='postgresql':
            self.backend.connection.execute('SET enable_seqscan=off')
            plan=self.backend.connection.execute('EXPLAIN '+query,('org','alice','cpu','engine')).fetchall()
            self.assertIn('admissions_owner',str(plan))
        else:
            plan=self.backend.connection.execute('EXPLAIN '+query,('org','alice','cpu','engine')).fetchall()
            self.assertIn('admissions_owner',str(plan))
        for literal in ('%','_','event-3 OR 1=1'):
            self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery(query=literal))['total'],0)

    def test_admission_relationships_and_evidence_cannot_be_changed(self):
        run=self.submit()
        for sql,args in [('DELETE FROM runs WHERE id=?',(run['id'],)),('UPDATE admissions SET subject=?',('bob',))]:
            with self.subTest(sql=sql),self.assertRaises(self.backend.database_errors):
                with self.backend.transaction():self.backend.connection.execute(sql,args)
        row=list(self.backend.connection.execute('SELECT * FROM admissions').fetchone())
        with self.backend.transaction():self.backend.connection.execute('DELETE FROM admissions')
        row[4]='bob'
        with self.assertRaises(self.backend.database_errors):
            with self.backend.transaction():self.backend.connection.execute('INSERT INTO admissions VALUES(?,?,?,?,?,?,?,?,?)',row)

    def test_large_audit_does_not_disable_search(self):
        self.submit()
        with self.backend.transaction():
            self.backend.connection.executemany('INSERT INTO journal("key",value) VALUES(?,?)',[(self.lifecycle.store._key('cpu','audit'),'{"operation":"archive"}')]*10001)
        self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery())['total'],1)

    def test_backup_roundtrip_preserves_rule_and_admission_evidence(self):
        run=self.submit()
        self.backend.maintenance('engine').pause()
        target=self.other_backend()
        restore(target,'engine',capture(self.backend,'engine'),source_stopped=True)
        self.assertEqual(target.repository('org','engine').get('cpu',self.draft['revision']),self.repository.get('cpu',self.draft['revision']))
        self.assertEqual(target.workflow_store('engine').get(run['id']),self.store.get(run['id']))
        self.assertEqual(target.connection.execute('SELECT subject,run_id FROM admissions').fetchone(),('alice',run['id']))
