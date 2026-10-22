"""Actual unknown effects, atomic attestation and current scoped authority."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from atropos.actions import Action, OutcomeUnknown
from atropos.authorization import Grant, OPERATIONS
from atropos.backend_registry import RedisBackend
from atropos.definitions import encode
from atropos.dispatch import digest
from atropos.reconciliation import Reconciler
from atropos.rule_lifecycle import Principal
from atropos.storage_contract import Conflict
from atropos.worker import Worker
from support import RedisFixture
import test_sqlite_rules as setup_helpers


def prepare(case, mode='real'):
    case.store.initialize()
    case.dispatcher.tests.workflows.store.initialize()
    case.effects = []
    async def unknown(params, context):
        case.effects.append((context.test_mode, context.target))
        raise OutcomeUnknown()
    case.actions['core.echo'] = Action(unknown, True, unknown)
    # A subsequent step must never be executed by reconciliation.
    case.binding['workflow']['steps']['first']['next'] = 'later'
    case.binding['workflow']['steps']['later'] = {'action': 'core.echo'}
    if mode == 'real':
        result = case.submit()
        store = case.store
    else:
        result = case.dispatcher.tests.submit(case.actor, 'cpu', ['server', 'host'],
                    case.facts, digest(case.binding), 'one')
        store = case.dispatcher.tests.workflows.store
    asyncio.run(Worker(store, case.actions, authorizer=lambda state: True,
                       test_mode=mode == 'test').run_once(0))
    case.assertEqual(store.get(result['id'])['status'], 'uncertain')
    return store, store.get(result['id']), Reconciler(case.dispatcher)


def exercise_resolution(case):
    for mode in ('real', 'test'):
        store, before, reconciler = prepare(case, mode)
        plan = reconciler.plan(case.actor, 'cpu', 'one', mode)
        case.assertEqual(plan['expected_state'], digest(before))
        case.assertEqual(store.get(before['id']), before)
        # A now-inactive rule and expired admission do not silently grant or
        # prevent a separately authorized operator attestation.
        if mode == 'real':
            case.lifecycle.deactivate(case.actor, 'cpu', expected_generation=case.active['generation'])
        result = reconciler.confirm(case.actor, 'cpu', 'one', mode=mode,
                    expected_state=plan['expected_state'], outcome='succeeded', evidence='synthetic-check-42')
        case.assertEqual(result['status'], 'succeeded')
        after = store.get(before['id'])
        for key in before:
            if key not in ('status', 'updated_at'):
                case.assertEqual(after[key], before[key], key)
        case.assertEqual(after['reconciliation']['author'], 'alice')
        case.assertEqual(after['reconciliation']['evidence'], 'synthetic-check-42')
        case.assertIsNone(store.client.get(store.target_key(before['target'])))
        case.assertEqual(case.lifecycle.audit(case.actor, 'cpu')[-1]['operation'], 'reconcile')
        case.assertEqual(case.lifecycle.audit(case.actor, 'cpu')[-1]['mode'], mode)
        asyncio.run(Worker(store, case.actions, authorizer=lambda state: True,
                           test_mode=mode == 'test').run_once(0))
        case.assertEqual(len(case.effects), 1)
        with case.assertRaises(Conflict):
            reconciler.confirm(case.actor, 'cpu', 'one', mode=mode,
                expected_state=plan['expected_state'], outcome='failed', evidence='duplicate')


def exercise_permissions(case):
    store, before, reconciler = prepare(case)
    plan = reconciler.plan(case.actor, 'cpu', 'one')
    original = case.grants
    for grant in (
        Grant('alice','org','intervention',OPERATIONS-{'reconcile'},{('server','host')},actions={'core.echo'}),
        Grant('alice','org','intervention',OPERATIONS, {('server','host')}),
        Grant('alice','org','intervention',OPERATIONS, {('server','other')},actions={'core.echo'}),
    ):
        case.grants = [grant]
        with case.assertRaises(PermissionError):
            reconciler.confirm(case.actor,'cpu','one',expected_state=plan['expected_state'],
                               outcome='succeeded',evidence='checked')
        case.assertEqual(store.get(before['id']), before)
    case.grants = original
    with patch.object(case.dispatcher,'identity_loader',return_value=None), case.assertRaises(PermissionError):
        reconciler.plan(case.actor,'cpu','one')
    for actor in (Principal('bob','org'),Principal('alice','other')):
        with case.assertRaises((KeyError, PermissionError)):
            reconciler.plan(actor,'cpu','one')
    # execute is not the reconciliation right.
    case.grants = [Grant('alice','org','intervention',{'read','reconcile'},
                        {('server','host')},actions={'core.echo'})]
    reconciler.confirm(case.actor,'cpu','one',expected_state=plan['expected_state'],
                      outcome='failed',evidence='checked externally')


def exercise_conflicts(case):
    store, before, reconciler = prepare(case)
    for token, outcome, evidence, error in (
        ('0'*64,'succeeded','checked',Conflict),
        (digest(before),'retry','checked',ValueError),
        (digest(before),'failed','',ValueError),
        (digest(before),'failed','x'*2049,ValueError),
        (digest(before),'failed','bad\nline',ValueError),
    ):
        with case.assertRaises(error):
            reconciler.confirm(case.actor,'cpu','one',expected_state=token,outcome=outcome,evidence=evidence)
        case.assertEqual(store.get(before['id']),before)
    target = store.target_key(before['target'])
    store.client.set(target,'other')
    with case.assertRaises(Conflict):
        reconciler.confirm(case.actor,'cpu','one',expected_state=digest(before),outcome='failed',evidence='checked')
    case.assertEqual(store.get(before['id']),before)
    case.assertEqual(store.client.get(target),'other')


def exercise_races(case):
    store,before,reconciler=prepare(case)
    original=reconciler._authorize
    calls=[]
    def revoke(*args):
        calls.append(1)
        if len(calls)==2:case.grants=[]
        return original(*args)
    grants=case.grants
    with patch.object(reconciler,'_authorize',side_effect=revoke),case.assertRaises(PermissionError):
        reconciler.confirm(case.actor,'cpu','one',expected_state=digest(before),outcome='failed',evidence='checked')
    case.assertEqual(store.get(before['id']),before)
    case.grants=grants
    resolve=store.reconcile
    changed=copy.deepcopy(before);changed['error']='changed_after_review'
    def race(*args,**kwargs):
        if hasattr(store,'_put'):
            with store.client.transaction():store._put(changed,store._row(before['id'])[1])
        else:store.client.set(store.key(before['id']),encode(changed))
        return resolve(*args,**kwargs)
    with patch.object(store,'reconcile',side_effect=race),case.assertRaises(Conflict):
        reconciler.confirm(case.actor,'cpu','one',expected_state=digest(before),outcome='failed',evidence='checked')
    case.assertEqual(store.get(before['id']),changed)
    case.assertEqual(store.client.get(store.target_key(before['target'])),before['id'])


class SQLiteReconciliationTests(unittest.TestCase):
    setUp = setup_helpers.SQLiteRuleTests.setUp
    configure_application = setup_helpers.SQLiteRuleTests.configure_application
    submit = setup_helpers.SQLiteRuleTests.submit

    def test_resolution_preserves_evidence_and_does_not_replay(self): exercise_resolution(self)
    def test_current_owner_scope_action_and_identity_rights(self): exercise_permissions(self)
    def test_stale_review_and_invalid_evidence_keep_uncertainty(self): exercise_conflicts(self)
    def test_policy_and_state_rechecked_before_commit(self): exercise_races(self)

    def test_audit_failure_rolls_back_state_and_target_release(self):
        store,before,reconciler=prepare(self)
        with patch.object(self.backend,'append',side_effect=OSError('synthetic')), self.assertRaises(OSError):
            reconciler.confirm(self.actor,'cpu','one',expected_state=digest(before),outcome='failed',evidence='checked')
        self.assertEqual(store.get(before['id']),before)
        self.assertEqual(store.client.get(store.target_key(before['target'])),before['id'])


class RedisReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture=RedisFixture()
    @classmethod
    def tearDownClass(cls): cls.fixture.close()
    def setUp(self):
        self.fixture.client.flushdb()
        self.backend=RedisBackend(self.fixture.client)
        setup_helpers.SQLiteRuleTests.configure_application(self)
    submit = setup_helpers.SQLiteRuleTests.submit

    def test_resolution_preserves_evidence_and_does_not_replay(self): exercise_resolution(self)
    def test_current_owner_scope_action_and_identity_rights(self): exercise_permissions(self)
    def test_stale_review_and_invalid_evidence_keep_uncertainty(self): exercise_conflicts(self)
    def test_policy_and_state_rechecked_before_commit(self): exercise_races(self)

    def test_wrong_type_audit_or_quarantine_never_partially_commits(self):
        store,before,reconciler=prepare(self)
        audit=self.lifecycle.store._key('cpu','audit')
        for key in (audit,store.quarantine,store.dead):
            saved=self.fixture.client.dump(key)
            self.fixture.client.delete(key);self.fixture.client.set(key,'wrong type')
            with self.assertRaises(Conflict):
                reconciler.confirm(self.actor,'cpu','one',expected_state=digest(before),outcome='failed',evidence='checked')
            self.assertEqual(store.get(before['id']),before)
            self.assertEqual(store.client.get(store.target_key(before['target'])),before['id'])
            self.fixture.client.delete(key)
            if saved:self.fixture.client.restore(key,0,saved)

    def test_active_lease_prevents_reconciliation(self):
        store,before,reconciler=prepare(self)
        self.fixture.client.set(store.lease(before['id']),'active',px=30000)
        with self.assertRaises(Conflict):
            reconciler.confirm(self.actor,'cpu','one',expected_state=digest(before),outcome='failed',evidence='checked')
        self.assertEqual(store.get(before['id']),before)
