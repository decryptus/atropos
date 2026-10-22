import asyncio
import unittest

from atropos.actions import ActionFailed, Context, OutcomeUnknown
from atropos.auton_adapter import auton_action


class AutonAdapterTests(unittest.TestCase):
    def context(self, target='node'):
        return Context('run-1', 'step', target, 1, 10)

    def factory(self, result, calls):
        class Service:
            def __init__(self, **kwargs):
                calls.append(kwargs)
            def execute_target(self, name, job_id, deadline, stopped, output_limit):
                calls.append({'job_id': job_id, 'name': name, 'output_limit': output_limit})
                return result
        return Service

    def test_stable_identity_and_explicit_configuration(self):
        calls = []
        action = auton_action('node', ['https://synthetic.invalid'], 'approved-job',
                              service_factory=self.factory({'status': 'completed', 'return_code': 0}, calls))
        first = asyncio.run(action.execute({'value': 1}, self.context()))
        second = asyncio.run(action.execute({'value': 1}, self.context()))
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(calls[0]['endpoint'], 'approved-job')
        self.assertEqual(calls[0]['targets'], {'bound-target': {'uris': ['https://synthetic.invalid']}})
        self.assertEqual(calls[1]['output_limit'], 0)
        self.assertFalse(action.retry_safe)

    def test_target_mismatch_rejected_before_submission(self):
        calls = []
        action = auton_action('node', [], 'job', service_factory=self.factory({}, calls))
        with self.assertRaises(ActionFailed):
            asyncio.run(action.execute({}, self.context('other')))
        self.assertEqual(calls, [])

    def test_canonical_resource_target_uses_a_local_client_alias(self):
        calls = []
        target = 'org/container/web'
        action = auton_action(target, ['https://synthetic.invalid'], 'approved-job',
            service_factory=self.factory({'status': 'completed', 'return_code': 0}, calls))
        asyncio.run(action.execute({}, self.context(target)))
        self.assertEqual(calls[0]['targets'], {'bound-target': {'uris': ['https://synthetic.invalid']}})
        self.assertEqual(calls[1]['name'], 'bound-target')

    def test_test_mode_does_not_contact_real_executor(self):
        calls = []
        action = auton_action('node', [], 'job', service_factory=self.factory({}, calls))
        with self.assertRaises(ActionFailed):
            asyncio.run(action.execute({}, Context('run-1', 'step', 'node', 1, 10, True, 'test-1')))
        self.assertEqual(calls, [])

    def test_unknown_and_malformed_results_remain_uncertain(self):
        for result in ({'status': 'unknown'}, {'status': 'completed', 'return_code': False}, None):
            action = auton_action('node', [], 'job', service_factory=self.factory(result, []))
            with self.subTest(result=result), self.assertRaises(OutcomeUnknown):
                asyncio.run(action.execute({}, self.context()))

    def test_confirmed_remote_failure(self):
        action = auton_action('node', [], 'job', service_factory=self.factory(
            {'status': 'failed', 'return_code': 2}, []))
        with self.assertRaises(ActionFailed):
            asyncio.run(action.execute({}, self.context()))
