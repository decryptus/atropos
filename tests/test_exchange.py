import copy
import hashlib
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atropos.actions import builtin_actions
from atropos.authorization import AccessPolicy, Grant
from atropos.exchange import AdmissionError, AdmissionPublisher, AdmissionReceiver, ApprovedWorkflows
from atropos.redis_store import RedisStore
from atropos.service import WorkflowService
from support import RedisFixture, workflow
from test_authorization import inventory


class ExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = RedisFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.store = RedisStore(self.fixture.client)
        self.store.initialize()
        self.actions = builtin_actions()
        self.definition = workflow()
        self.catalog = ApprovedWorkflows([self.definition], self.actions)
        self.policy = AccessPolicy([
            Grant('alice', 'org', 'intervention', frozenset({'execute'}),
                  frozenset({('container_group', 'api')}), frozenset({'core.echo'}))], inventory())
        self.key = Ed25519PrivateKey.generate()
        self.publisher = AdmissionPublisher(self.policy, self.catalog, self.key, 'key-1', 'atraxis', 'atropos')
        self.receiver = AdmissionReceiver(WorkflowService(self.store, self.actions), self.policy,
            self.catalog, {'key-1': self.key.public_key()}, 'atraxis', 'atropos')
        self.request = {'workflow': self.definition['name'], 'revision': self.catalog.revision(self.definition),
                        'target': {'kind': 'container', 'id': 'api-a'}, 'input': {}, 'dedup_key': 'incident-1'}

    def prepared(self):
        return self.publisher.prepare('alice', 'org', self.request)

    def modified_token(self, token, **overrides):
        claims = jwt.decode(token, options={'verify_signature': False})
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm='EdDSA', headers={'kid': 'key-1', 'typ': 'JWT'})

    def test_authenticated_admission_records_actor_revision_and_canonical_target(self):
        body, token = self.prepared()
        receipt = self.receiver.submit(body, token)
        state = self.store.get(receipt['id'])
        self.assertEqual(state['status'], 'queued')
        self.assertEqual(state['target'], 'org/container/api-a')
        self.assertEqual(state['attribution'], {'subject': 'alice', 'organization': 'org',
                         'issuer': 'atraxis', 'workflow_revision': self.request['revision']})

    def test_publisher_checks_authenticated_user_permissions(self):
        with self.assertRaises(AdmissionError):
            self.publisher.prepare('bob', 'org', self.request)
        self.assertEqual(self.fixture.client.xlen(self.store.stream), 0)

    def test_body_tampering_rejected(self):
        body, token = self.prepared()
        changed = body.replace(b'api-a', b'db-a')
        with self.assertRaisesRegex(AdmissionError, 'unauthorized'):
            self.receiver.submit(changed, token)
        self.assertEqual(self.fixture.client.xlen(self.store.stream), 0)

    def test_replay_is_rejected_but_fresh_ticket_deduplicates(self):
        body, token = self.prepared()
        first = self.receiver.submit(body, token)
        with self.assertRaisesRegex(AdmissionError, 'replay_rejected'):
            self.receiver.submit(body, token)
        second = self.receiver.submit(*self.prepared())
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.fixture.client.xlen(self.store.stream), 1)

    def test_concurrent_replay_has_one_admission(self):
        body, token = self.prepared()
        def attempt(_):
            try:
                return self.receiver.submit(body, token)
            except AdmissionError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.fixture.client.xlen(self.store.stream), 1)

    def test_expired_future_wrong_audience_and_wrong_issuer_rejected(self):
        body, token = self.prepared()
        now = int(time.time())
        for changes in ({'exp': now - 1}, {'nbf': now + 120, 'iat': now + 120, 'exp': now + 180},
                        {'aud': 'other'}, {'iss': 'other'}, {'exp': now + 3600}, {'iat': True}):
            with self.subTest(changes=changes), self.assertRaises(AdmissionError):
                self.receiver.submit(body, self.modified_token(token, **changes))

    def test_untrusted_key_unsigned_token_and_algorithm_confusion_rejected(self):
        body, token = self.prepared()
        claims = jwt.decode(token, options={'verify_signature': False})
        invalid = [jwt.encode(claims, Ed25519PrivateKey.generate(), algorithm='EdDSA', headers={'kid': 'key-1'}),
                   jwt.encode(claims, self.key, algorithm='EdDSA', headers={'kid': 'missing'}),
                   jwt.encode(claims, None, algorithm='none', headers={'kid': 'key-1'}),
                   jwt.encode(claims, b'x' * 32, algorithm='HS256', headers={'kid': 'key-1'})]
        for value in invalid:
            with self.assertRaises(AdmissionError):
                self.receiver.submit(body, value)

    def test_signed_identity_and_organization_still_require_receiver_permissions(self):
        body, token = self.prepared()
        for changes in ({'sub': 'bob'}, {'org': 'other'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(AdmissionError, 'forbidden'):
                self.receiver.submit(body, self.modified_token(token, **changes))

    def test_receiver_policy_revocation_applies_before_admission(self):
        body, token = self.prepared()
        self.receiver.policy = AccessPolicy([], inventory())
        with self.assertRaisesRegex(AdmissionError, 'forbidden'):
            self.receiver.submit(body, token)

    def test_unapproved_revision_and_client_supplied_groups_rejected(self):
        for change in ('revision', 'groups'):
            request = copy.deepcopy(self.request)
            if change == 'revision':
                request['revision'] = '0' * 64
            else:
                request['target']['groups'] = ['api']
            with self.subTest(change=change), self.assertRaises(AdmissionError):
                self.publisher.prepare('alice', 'org', request)

    def test_method_path_and_key_revocation(self):
        body, token = self.prepared()
        for args in ({'method': 'GET'}, {'path': '/v1/other'}):
            with self.assertRaises(AdmissionError):
                self.receiver.submit(body, token, **args)
        self.receiver.public_keys.clear()
        with self.assertRaises(AdmissionError):
            self.receiver.submit(body, token)

    def test_nonce_consumed_before_failure_can_be_retried_with_new_ticket(self):
        body, token = self.prepared()
        original = self.receiver.service.submit
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic interruption after nonce check')
        self.receiver.service.submit = fail
        with self.assertRaises(RuntimeError):
            self.receiver.submit(body, token)
        self.receiver.service.submit = original
        with self.assertRaisesRegex(AdmissionError, 'replay_rejected'):
            self.receiver.submit(body, token)
        receipt = self.receiver.submit(*self.prepared())
        self.assertEqual(receipt['status'], 'queued')
