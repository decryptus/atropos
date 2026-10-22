"""Engine-owned composition for approved dispatch and guarded workers."""
import asyncio
from contextlib import contextmanager
from pathlib import Path

import yaml

from atropos_client.config import StrictLoader
from atropos_client.files import read_private
from .execution_adapters import configured_actions
from .dispatch import ApprovedBindings, DecisionDispatcher, DispatchAuthorizer
from .backend_registry import RedisBackend, open_storage, open_queue, destination
from .rule_lifecycle import RuleLifecycle
from .service import WorkflowService
from .worker import Worker

MAX_BINDING_FILE_BYTES = 1048576


class RuleExecutionRuntime:
    def __init__(self, client, namespace, configuration_loader, config_path):
        self.backend = client if hasattr(client, 'workflow_store') else RedisBackend(client)
        self.client, self.namespace = self.backend.client, namespace
        self.configuration_loader = configuration_loader
        self.config_path = Path(config_path)
        document, _ = configuration_loader()
        self.adapter_specs = document.get('execution_adapters', {})
        self.actions = configured_actions(self.adapter_specs, self.config_path, configuration_loader)
        queue = document.get('queue', {'backend': 'storage'})
        self.store = self.backend.workflow_store(namespace, open_queue(queue, namespace))
        from .test_execution import test_namespace
        self.test_store = self.backend.workflow_store(test_namespace(namespace), open_queue(queue, test_namespace(namespace)))

    def bindings(self):
        document, _ = self.configuration_loader()
        if document.get('execution_adapters', {}) != self.adapter_specs:
            raise ValueError('execution_adapters_changed_restart_required')
        filename = document.get('dispatch_bindings')
        if filename is None:
            return ApprovedBindings([], self.actions)
        path = Path(filename)
        if not path.is_absolute():
            path = self.config_path.parent / path
        try:
            records = yaml.load(read_private(path, MAX_BINDING_FILE_BYTES), Loader=StrictLoader)
            return ApprovedBindings(records, self.actions)
        except (yaml.YAMLError, UnicodeError, RecursionError, TypeError):
            raise ValueError('invalid_dispatch_bindings') from None

    def observation_keys(self):
        from atropos_client.observation_signing import MAX_KEY_BYTES
        document, _ = self.configuration_loader()
        result = {}
        for key, filename in document.get('observation_keys', {}).items():
            path = Path(filename)
            if not path.is_absolute(): path = self.config_path.parent / path
            result[key] = read_private(path, MAX_KEY_BYTES)
        return result

    def observation_check(self, state, binding):
        from .observation_authority import verify_run_observation
        return verify_run_observation(state, binding, self.observation_keys(), self.store.now())

    def identity_valid(self, actor, credential_id):
        document, policy = self.configuration_loader()
        if not policy.member(actor.subject, actor.organization):
            return False
        return any(credential_id == ('bearer:' + entry['token_sha256'] if 'token_sha256' in entry
                                     else 'uid:' + str(entry['uid']))
                   and actor.subject == entry['subject'] and actor.organization == entry['organization']
                   for entry in document['identities'])

    def lifecycle(self, actor):
        repository = self.backend.repository(actor.organization, self.namespace)
        return RuleLifecycle(repository, self.backend.lifecycle_store(repository), lambda: self.configuration_loader()[1])

    def dispatcher(self, lifecycle, identity_loader):
        dispatcher = DecisionDispatcher(lifecycle, WorkflowService(self.store, self.actions), self.bindings, identity_loader)
        from .observation_authority import ObservationAuthority
        dispatcher.observations = ObservationAuthority(dispatcher, self.observation_keys)
        from .test_execution import RuleTestRunner
        dispatcher.tests = RuleTestRunner(dispatcher, WorkflowService(self.test_store, self.actions))
        return dispatcher

    def worker(self, test_mode=False):
        if test_mode:
            from .test_execution import TestRunAuthorizer
            from .test_alert_delivery import configured_test_alert
            guard = TestRunAuthorizer(self.lifecycle, self.bindings, self.identity_valid, self.test_store.now)
            actions = dict(self.actions, **{'core.alert': configured_test_alert(self.configuration_loader, self.config_path)})
            return Worker(self.test_store, actions, authorizer=guard, require_authorization=True, test_mode=True)
        guard = DispatchAuthorizer(self.lifecycle, self.bindings, self.identity_valid, self.store.now, self.observation_check)
        return Worker(self.store, self.actions, authorizer=guard, require_authorization=True)


@contextmanager
def configured_runtime(path):
    from .rule_operator import load_operator_config
    path = Path(path).absolute()
    initial, _ = load_operator_config(path, remote=True)
    initial_destination = destination(initial)

    def configuration():
        document, policy = load_operator_config(path, remote=True)
        if destination(document) != initial_destination:
            raise ValueError('worker_destination_changed_restart_required')
        return document, policy

    client = open_storage(initial['storage'])
    try:
        runtime = RuleExecutionRuntime(client, initial['namespace'], configuration, path)
        yield runtime
    finally:
        client.close()


def run_rule_worker(path, *, once=False, test_mode=False):
    with configured_runtime(path) as runtime:
        runtime.bindings()  # Validate trusted binding configuration before polling.
        (runtime.test_store if test_mode else runtime.store).initialize()
        worker = runtime.worker(test_mode)
        asyncio.run(worker.run_once(0) if once else worker.run())


def list_bindings(path):
    with configured_runtime(path) as runtime:
        return [{'id': key, 'organization': value['organization'], 'rule': value['rule'],
                 'revision': value['revision'], 'result': value['result'],
                 'workflow': value['workflow']['name'], 'max_age_seconds': value['max_age_seconds']}
                for key, value in runtime.bindings().records.items()]
