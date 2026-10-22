import asyncio
import copy
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atropos_client.observation_signing import sign_observation
from atropos.dispatch import ApprovedBindings, DispatchAuthorizer, digest
from atropos.observation_authority import ObservationAuthority, verify_run_observation
from atropos.redis_store import Conflict
from atropos.worker import Worker

root=Path(__file__).resolve().parents[1]
# Reuse only the disposable fixture/arrangement, not its TestCase declarations.
import sys
sys.path.insert(0,str(root/'tests'))
import test_dispatch as arrangement


class ObservedDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture=arrangement.RedisFixture()
    @classmethod
    def tearDownClass(cls): cls.fixture.close()

    def setUp(self):
        self.case=arrangement.DispatchTests();self.case.fixture=self.fixture;self.case.setUp()
        c=self.case
        self.private=Ed25519PrivateKey.generate();self.keys={'gateway':self.private.public_key()}
        c.binding['observation']={'key_id':'gateway','source_id':'monit','native_id':'a'*64,
                                'filesystem':None,'max_age_seconds':60}
        self.authority=ObservationAuthority(c.dispatcher,lambda:self.keys)
        c.guard.observation_check=lambda state,binding:verify_run_observation(state,binding,self.keys,c.store.now())

    def proof(self, *, age=1, source=None):
        c=self.case
        challenge=self.authority.challenge(c.actor,'cpu',['container','web'],digest(c.binding))
        payload={'challenge':challenge,'facts':c.facts,
                 'source':source or {'source_id':'monit','native_id':'a'*64,'filesystem':None},
                 'age_seconds':age,'max_age_seconds':60}
        return sign_observation(self.private,'gateway',payload),payload

    def dispatch(self,proof):
        c=self.case
        return self.authority.dispatch(c.actor,proof,c.active['generation'],'observed-1')

    def test_signed_fresh_observation_reaches_queue_and_guarded_worker(self):
        c=self.case;proof,_=self.proof()
        preview=self.authority.plan(c.actor,proof)
        self.assertEqual(preview['facts'],c.facts);self.assertEqual(c.store.client.xlen(c.store.stream),0)
        receipt=self.dispatch(proof);c.step();c.step()
        self.assertEqual(c.store.get(receipt['id'])['status'],'succeeded')
        self.assertEqual(len(c.calls),2)
        self.assertNotIn(proof,str(receipt))
        self.assertTrue(receipt['observation']['verified_at_admission'])

    def test_manual_values_cannot_bypass_required_observation(self):
        c=self.case
        with self.assertRaises(PermissionError):c.submit()
        with self.assertRaises(PermissionError):c.dispatcher.plan(c.actor,'cpu',['container','web'],c.facts,digest(c.binding))
        self.assertEqual(c.store.client.xlen(c.store.stream),0)

    def test_wrong_source_native_id_or_filesystem_rejected(self):
        for source in ({'source_id':'other','native_id':'a'*64,'filesystem':None},
                       {'source_id':'monit','native_id':'b'*64,'filesystem':None},
                       {'source_id':'monit','native_id':'a'*64,'filesystem':['data','/wrong']}):
            with self.subTest(source=source):
                proof,_=self.proof(source=source)
                with self.assertRaises(PermissionError):self.dispatch(proof)
        self.assertEqual(self.case.store.client.xlen(self.case.store.stream),0)

    def test_untrusted_key_modified_facts_and_unapproved_headers_rejected(self):
        import jwt
        proof,payload=self.proof()
        bad=sign_observation(Ed25519PrivateKey.generate(),'gateway',payload)
        with self.assertRaises(PermissionError):self.dispatch(bad)
        bad=jwt.encode(payload,self.private,algorithm='EdDSA',headers={'kid':'gateway','jku':'https://example.invalid'})
        with self.assertRaises(PermissionError):self.dispatch(bad)
        parts=proof.split('.');parts[1]=parts[1][:-1]+('a' if parts[1][-1]!='a' else 'b')
        with self.assertRaises(PermissionError):self.dispatch('.'.join(parts))

    def test_challenge_is_credential_bound_and_single_use(self):
        c=self.case;proof,_=self.proof()
        credential=c.dispatcher.identity_loader
        c.dispatcher.identity_loader=lambda actor:'other-credential'
        with self.assertRaises(PermissionError):self.dispatch(proof)
        c.dispatcher.identity_loader=credential
        receipt=self.dispatch(proof)
        with self.assertRaises(PermissionError):self.dispatch(proof)
        self.assertEqual(c.dispatcher.status(c.actor,'cpu','observed-1')['id'],receipt['id'])
        self.assertEqual(c.store.client.xlen(c.store.stream),1)

    def test_new_challenge_invalidates_prior_evidence_without_unbounded_slots(self):
        c=self.case;old,_=self.proof();new,_=self.proof()
        with self.assertRaises(PermissionError):self.dispatch(old)
        self.dispatch(new)
        self.assertEqual(len(c.store.client.keys('*observation-challenge*')),0)

    def test_stale_negative_and_nonfinite_ages_are_rejected(self):
        for age in (60,-1,True):
            with self.subTest(age=age):
                proof,_=self.proof(age=age)
                with self.assertRaises((PermissionError,ValueError)):self.dispatch(proof)
        proof,payload=self.proof();payload['age_seconds']=float('nan')
        with self.assertRaises(ValueError):sign_observation(self.private,'gateway',payload)

    def test_expiry_during_evaluation_prevents_queue_insertion(self):
        proof,_=self.proof(age=59.9);c=self.case
        original=c.dispatcher.workflows.submit
        def slow(*args,**kwargs):
            time.sleep(.12)
            return original(*args,**kwargs)
        with patch.object(c.dispatcher.workflows,'submit',side_effect=slow),self.assertRaises(Conflict):self.dispatch(proof)
        self.assertEqual(c.store.client.xlen(c.store.stream),0)

    def test_challenge_change_during_admission_fails_atomically(self):
        proof,_=self.proof();c=self.case;original=c.dispatcher.workflows.submit
        def change(*args,**kwargs):
            self.proof()
            return original(*args,**kwargs)
        with patch.object(c.dispatcher.workflows,'submit',side_effect=change),self.assertRaises(Conflict):self.dispatch(proof)
        self.assertEqual(c.store.client.xlen(c.store.stream),0)
        self.assertEqual(len(c.lifecycle.audit(c.actor,'cpu')),1)

    def test_key_revocation_or_measurement_expiry_before_worker_cancels(self):
        for change in ('key','age'):
            with self.subTest(change=change):
                self.setUp();c=self.case;proof,_=self.proof();receipt=self.dispatch(proof)
                if change=='key':self.keys={}
                else:c.guard.observation_check=lambda state,binding:verify_run_observation(state,binding,self.keys,c.store.now()+61)
                c.step();self.assertEqual(c.store.get(receipt['id'])['status'],'cancelled');self.assertEqual(c.calls,[])

    def test_reactivation_with_same_revision_invalidates_old_preview(self):
        c=self.case;proof,_=self.proof()
        c.lifecycle.activate(c.actor,'cpu',c.revision,test_id=c.active['test_id'],expected_generation=c.active['generation'])
        with self.assertRaises(Conflict):self.authority.plan(c.actor,proof)
        with self.assertRaises(Conflict):self.dispatch(proof)
