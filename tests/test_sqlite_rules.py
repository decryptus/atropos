import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atropos.actions import builtin_actions
from atropos.authorization import AccessPolicy, Grant, OPERATIONS, Resource
from atropos.backend_registry import open_storage
from atropos.dispatch import ApprovedBindings, DecisionDispatcher, DispatchAuthorizer, digest
from atropos.intervention_search import InterventionQuery, search
from atropos.rule_catalog import instantiate
from atropos.rule_lifecycle import Principal, RuleLifecycle
from atropos.service import WorkflowService
from atropos.storage_contract import Conflict
from atropos.test_execution import RuleTestRunner, test_namespace
from atropos.worker import Worker
from support import workflow


class SQLiteRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.backend=open_storage({'backend':'sqlite','url':'sqlite:///'+self.directory.name+'/state.db'})
        self.addCleanup(self.backend.close)
        self.configure_application()

    def configure_application(self):
        self.actor=Principal('alice','org')
        self.grants=[Grant('alice','org','intervention',OPERATIONS,{('server','host')},actions={'core.echo'},providers={'internal'})]
        self.policy=lambda:AccessPolicy(self.grants,[Resource('org','server','host')])
        self.repository=self.backend.repository('org','engine')
        self.lifecycle=RuleLifecycle(self.repository,self.backend.lifecycle_store(self.repository),self.policy)
        self.document=instantiate(['cpu-high'],name='cpu')
        self.draft=self.lifecycle.save_draft(self.actor,self.document,rule_kind='intervention',selections=[('server','host')],expected_revision=None)
        self.facts={'cpu_percent':95};self.result={'decision':'review','matched_rule':'combined'}
        self.report=self.lifecycle.test(self.actor,'cpu',self.draft['revision'],[dict(name='hot',input=self.facts,expected=self.result)])
        self.active=self.lifecycle.activate(self.actor,'cpu',self.draft['revision'],test_id=self.report['id'],expected_generation=None)
        self.actions=builtin_actions();self.store=self.backend.workflow_store('engine')
        self.binding=dict(organization='org',rule='cpu',revision=self.draft['revision'],result=self.result,workflow=workflow(),input={},max_age_seconds=60)
        self.bindings=lambda:ApprovedBindings([self.binding],self.actions)
        self.dispatcher=DecisionDispatcher(self.lifecycle,WorkflowService(self.store,self.actions),self.bindings,lambda actor:'synthetic')
        self.dispatcher.tests=RuleTestRunner(self.dispatcher,WorkflowService(self.backend.workflow_store(test_namespace('engine')),self.actions))

    def submit(self,event='one'):
        return self.dispatcher.dispatch(self.actor,'cpu',['server','host'],self.facts,digest(self.binding),self.active['generation'],event)

    def test_guarded_dispatch_search_and_results_survive_reopen(self):
        run=self.submit();self.assertEqual(self.submit()['id'],run['id'])
        guard=DispatchAuthorizer(lambda actor:self.lifecycle,self.bindings,lambda actor,credential:True,self.store.now)
        asyncio.run(Worker(self.store,self.actions,authorizer=guard,require_authorization=True).run_once(0))
        found=search(self.dispatcher,self.actor,'cpu',InterventionQuery(status='succeeded',target='HOST'))
        self.assertEqual(found['total'],1);self.assertNotIn('input',found['items'][0])
        self.assertEqual(self.store.get(run['id'])['status'],'succeeded')
        self.assertEqual(len(self.lifecycle.store.admission_history('cpu',10)),2)

    def test_search_is_owner_scoped_revocable_and_separates_test_work(self):
        self.submit()
        self.dispatcher.tests.submit(self.actor,'cpu',['server','host'],self.facts,digest(self.binding),'test-event')
        self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery())['total'],2)
        self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery(mode='test'))['total'],1)
        self.grants=[Grant('alice','org','intervention',{'read'},{('server','host')})]
        self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery())['total'],1)
        from atropos.sql_search import SQLInterventionIndex
        original=SQLInterventionIndex.page
        def revoke(index,*args):
            value=original(index,*args);self.grants=[];return value
        with patch.object(SQLInterventionIndex,'page',revoke),self.assertRaises(PermissionError):
            search(self.dispatcher,self.actor,'cpu',InterventionQuery())

    def test_atomic_generation_checks_and_linked_evidence_prevent_delete(self):
        self.submit()
        self.lifecycle.deactivate(self.actor,'cpu',expected_generation=self.active['generation'])
        with self.assertRaises(Conflict):self.submit('stale')
        args=dict(expected_revision=self.draft['revision'],expected_generation=None)
        with self.assertRaises(Conflict):self.lifecycle.delete(self.actor,'cpu',**args)
        archived=self.lifecycle.archive(self.actor,'cpu',**args)
        self.assertEqual(archived['status'],'archived')
        restored=self.lifecycle.unarchive(self.actor,'cpu',expected_revision=self.draft['revision'],expected_generation=archived['generation'])
        self.assertEqual(restored['status'],'available')
        self.assertIsNone(self.lifecycle.active(self.actor,'cpu')['revision'])

    def test_draft_delete_preserves_audit_and_revision_cas(self):
        doc=instantiate(['cpu-high'],name='draft')
        draft=self.lifecycle.save_draft(self.actor,doc,rule_kind='intervention',selections=[('server','host')],expected_revision=None)
        with self.assertRaises(Conflict):self.repository.save(doc,'alice',None)
        deleted=self.lifecycle.delete(self.actor,'draft',expected_revision=draft['revision'],expected_generation=None)
        self.assertEqual(deleted['status'],'deleted')
        self.assertEqual(self.lifecycle.audit(self.actor,'draft')[-1]['operation'],'delete')
        with self.assertRaises(KeyError):self.repository.get('draft',draft['revision'])

    def test_indexed_search_does_not_load_run_payloads_or_scan_audit(self):
        for i in range(4):self.submit('event-'+str(i))
        with patch.object(self.store,'get',side_effect=AssertionError('full payload read')),\
             patch.object(self.lifecycle.store,'admission_history',side_effect=AssertionError('audit scan')):
            found=search(self.dispatcher,self.actor,'cpu',InterventionQuery(query='event-3',limit=1))
        self.assertEqual(found['total'],1)
        self.assertEqual(found['items'][0]['event_id'],'event-3')
        query="SELECT run_id FROM admissions WHERE organization=? AND subject=? AND rule=? AND namespace=?"
        plan=list(self.backend.connection.execute('EXPLAIN QUERY PLAN '+query,('org','alice','cpu','engine')))
        self.assertTrue(any('admissions_owner' in row[3] for row in plan))

    def test_admission_foreign_keys_and_immutable_evidence(self):
        import sqlite3
        run=self.submit()
        for sql,args in [('DELETE FROM runs WHERE id=?',(run['id'],)),
                         ('UPDATE admissions SET subject=?',('bob',)),
                         ('UPDATE admissions SET revision=?',('a'*31,))]:
            with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):
                with self.backend.transaction():self.backend.connection.execute(sql,args)
        self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery())['total'],1)

    def test_history_above_previous_capacity_does_not_disable_indexed_search(self):
        self.submit()
        with self.backend.transaction():
            self.backend.connection.executemany('INSERT INTO journal(key,value) VALUES(?,?)',
                [(self.lifecycle.store._key('cpu','audit'),'{"operation":"archive"}')]*10001)
        self.assertEqual(search(self.dispatcher,self.actor,'cpu',InterventionQuery())['total'],1)

    def test_missing_authorized_evidence_is_not_hidden_by_filters(self):
        run=self.submit()
        # Model a damaged database written by an external connection with FKs off.
        self.backend.connection.execute('PRAGMA foreign_keys=OFF')
        self.backend.connection.execute('DELETE FROM runs WHERE id=?',(run['id'],))
        self.backend.connection.execute('PRAGMA foreign_keys=ON')
        with self.assertRaisesRegex(ValueError,'invalid_intervention_evidence'):
            search(self.dispatcher,self.actor,'cpu',InterventionQuery(query='no-match'))
