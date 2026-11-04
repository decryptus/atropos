import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atropos.actions import Context
from atropos.worker import ExecutionDenied
from atropos.dispatch import ApprovedBindings
from atropos.execution_adapters import adapter_revision, configured_actions, validate_adapters


class ExecutionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root / 'engine.yml'
        self.spec = dict(backend='auton', target='org/container/web', origin='https://executor.example',
                        endpoint='approved', username='operator', password_file='password', simulation='noop')
        self.resources = [dict(organization='org', kind='container', id='web')]
        self.document = {'execution_adapters': {'ops.approved': self.spec}}
        self.loader = lambda: (deepcopy(self.document), None)

    def test_configuration_rejects_untrusted_destinations_overrides_and_scopes(self):
        validate_adapters(self.document['execution_adapters'], self.resources)
        for values in [dict(backend='shell'), dict(target='org/server/other'), dict(origin='http://remote.example'),
                       dict(origin='https://user:password@executor.example'), dict(origin='https://executor.example/path'),
                       dict(origin='https://executor.example', allow_loopback_http=True), dict(endpoint='../x'),
                       dict(password_file=''), dict(username='operator:other'), dict(verify=False), dict(simulation='real')]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_adapters({'ops.approved': dict(self.spec, **values)}, self.resources)
        with self.assertRaises(ValueError): validate_adapters({'core.echo': self.spec}, self.resources)
        local = dict(self.spec, origin='http://127.0.0.1:9000', allow_loopback_http=True)
        validate_adapters({'ops.approved': local}, self.resources)

    def test_simulator_never_opens_credentials_or_contacts_executor(self):
        action = configured_actions(self.document['execution_adapters'], self.path, self.loader)['ops.approved']
        context = Context('run', 'step', 'org/container/web', 1, 5, True, 'test-id')
        with patch('atropos.execution_adapters.read_private', side_effect=AssertionError('no secrets in simulation')):
            result = asyncio.run(action.simulate({}, context))
        self.assertEqual(result['test_context'], {'test': True, 'id': 'test-id'})
        self.assertEqual(result['effects'], 'none')
        with self.assertRaises(ExecutionDenied): asyncio.run(action.execute({}, context))

    def test_changed_adapter_or_missing_secret_fails_before_submission(self):
        action = configured_actions(self.document['execution_adapters'], self.path, self.loader)['ops.approved']
        context = Context('run', 'step', 'org/container/web', 1, 5)
        with self.assertRaises(ExecutionDenied): asyncio.run(action.execute({}, context))
        password = self.root / 'password'; password.write_text('synthetic-secret'); password.chmod(0o644)
        with self.assertRaises(ExecutionDenied): asyncio.run(action.execute({}, context))
        password.chmod(0o600)
        self.document['execution_adapters']['ops.approved']['endpoint'] = 'other'
        with self.assertRaises(ExecutionDenied): asyncio.run(action.execute({}, context))
        with self.assertRaises(ExecutionDenied):
            asyncio.run(action.simulate({}, Context('run', 'step', 'org/container/web', 1, 5, True, 'test-id')))

    def test_bearer_credentials_exclusive_private_and_reloaded(self):
        import requests
        from atropos.actions import Action
        self.spec.pop('username')
        self.spec.pop('password_file')
        for fields in ({}, {'token_file': 'token', 'username': 'operator'},
                       {'token_file': 'token', 'password_file': 'password'}):
            with self.assertRaises(ValueError):
                validate_adapters({'ops.approved': dict(self.spec, **fields)}, self.resources)
        self.spec['token_file'] = 'token'
        validate_adapters(self.document['execution_adapters'], self.resources)
        action = configured_actions(self.document['execution_adapters'], self.path, self.loader)['ops.approved']
        context = Context('run', 'step', self.spec['target'], 1, 5)
        token = self.root / 'token'
        async def execute(params, context):
            return {}
        with patch('atropos.auton_adapter.auton_action', return_value=Action(execute)) as submit:
            token.write_text('a' * 43)
            token.chmod(0o644)
            with self.assertRaises(ExecutionDenied):
                asyncio.run(action.execute({}, context))
            submit.assert_not_called()
            token.chmod(0o600)
            for value in ('a' * 43, 'b' * 43):
                token.write_text(value)
                asyncio.run(action.execute({}, context))
                auth = submit.call_args.kwargs['auth']
                req = requests.Request('GET', self.spec['origin'], auth=auth).prepare()
                self.assertEqual(req.headers['Authorization'], 'Bearer ' + value)

    def test_reviewed_binding_pins_exact_adapter_configuration(self):
        actions = configured_actions(self.document['execution_adapters'], self.path, self.loader)
        binding = dict(organization='org', rule='cpu', revision='a'*32, result={'decision':'yes','matched_rule':'hot'},
            input={}, max_age_seconds=60, workflow={'version':1,'name':'approved','start':'run',
            'steps':{'run':{'action':'ops.approved'}}})
        with self.assertRaisesRegex(ValueError, 'revision_mismatch'): ApprovedBindings([binding], actions)
        binding['adapter_revisions'] = {'ops.approved': adapter_revision(self.spec)}
        ApprovedBindings([binding], actions)
        self.spec['endpoint'] = 'changed'
        changed = configured_actions(self.document['execution_adapters'], self.path, self.loader)
        with self.assertRaisesRegex(ValueError, 'revision_mismatch'): ApprovedBindings([binding], changed)

    def test_explicit_credentials_tls_and_no_ambient_transport(self):
        password=self.root/'password';password.write_text('synthetic-secret\n');password.chmod(0o600)
        ca=self.root/'ca.pem';ca.write_text('synthetic CA fixture');ca.chmod(0o600)
        self.spec['ca_file']='ca.pem'
        action=configured_actions(self.document['execution_adapters'],self.path,self.loader)['ops.approved']
        captured={}
        async def execute(params,context):return {'confirmed':True}
        from atropos.actions import Action
        def factory(*args,**kwargs):captured.update(kwargs);return Action(execute)
        with patch('atropos.auton_adapter.auton_action',side_effect=factory):
            asyncio.run(action.execute({},Context('run','step','org/container/web',1,5)))
        self.assertEqual(captured['auth'],('operator','synthetic-secret'))
        self.assertEqual(captured['transport'],{'verify':str(ca)})
        self.assertFalse(captured['client_factory'].keywords['session'].trust_env)

    def test_worker_cancels_refused_execution_before_error_branch(self):
        from atropos.backend_registry import open_storage
        from atropos.service import WorkflowService
        from atropos.worker import Worker

        backend = open_storage(dict(backend='sqlite', url='sqlite:///' + str(self.root / 'state.db')))
        self.addCleanup(backend.close)
        store = backend.workflow_store('engine')
        actions = configured_actions(self.document['execution_adapters'], self.path, self.loader)
        workflow = dict(version=1, name='review', start='run',
                        steps={'run': dict(action='ops.approved', on_error='fallback'),
                               'fallback': dict(action='core.echo')})
        service = WorkflowService(store, actions)
        worker = Worker(store, actions, authorizer=lambda state: True, require_authorization=True)
        for reason, target in [('target mismatch', 'org/container/other'),
                               ('missing password', self.spec['target']),
                               ('removed adapter', self.spec['target'])]:
            with self.subTest(reason=reason):
                password = self.root / 'password'
                if reason != 'missing password':
                    password.write_text('synthetic-secret')
                    password.chmod(0o600)
                else:
                    password.unlink()
                if reason == 'removed adapter':
                    self.document['execution_adapters'].clear()
                run = service.submit(workflow, {}, target)
                with patch('atropos.auton_adapter.auton_action') as submit:
                    asyncio.run(worker.run_once(0))
                    submit.assert_not_called()
                state = store.get(run['id'])
                self.assertEqual(state['status'], 'cancelled')
                self.assertEqual(state['attempts'], {'run': 1})
