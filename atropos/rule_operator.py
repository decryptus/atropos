"""Explicit local trusted-operator configuration, separate from daemon settings.

OS login identifies the operator. Whoever can edit this file or access its backend
credentials is a trusted administrator; this is not remote user authentication.
"""
import os
from pathlib import Path
import re

import yaml

from atropos_client.config import ConfigurationError, StrictLoader as _StrictLoader
from .rule_application import RuleActor, build_rule_application
from atropos_client.files import read_private

MAX_OPERATOR_BYTES = 262144
MAX_IDENTITIES = 128
REQUIRED = frozenset({'version', 'namespace', 'identities', 'resources', 'grants'})
OPTIONAL = frozenset({'storage', 'queue', 'dispatch_bindings', 'observation_keys', 'test_alert_delivery', 'execution_adapters', 'membership'})
IDENTITY_FIELDS = frozenset({'uid', 'subject', 'organization'})
RESOURCE_FIELDS = frozenset({'organization', 'kind', 'id', 'server_id', 'groups'})
GRANT_FIELDS = frozenset({'subject', 'organization', 'rule_kind', 'operations', 'scopes', 'actions', 'providers', 'subject_kind'})


def _require(condition):
    if not condition:
        raise ConfigurationError('invalid_rule_operator_configuration')


def load_operator_config(path, *, remote=False):
    """No symlink final component, private regular file, owned by root/current UID."""
    try:
        raw = read_private(path, MAX_OPERATOR_BYTES)
        document = yaml.load(raw, Loader=_StrictLoader)
        _require(isinstance(document, dict) and REQUIRED <= set(document)
                 and set(document) <= REQUIRED | OPTIONAL and type(document['version']) is int
                 and document['version'] == 1)
        from atropos.authorization import AccessPolicy, Grant, Resource, identifier
        from atropos.definitions import name
        name(document['namespace'], 'namespace')
        from .backend_registry import validate_configuration
        _require('storage' in document)
        for role in ('storage', 'queue'):
            if role not in document: continue
            validate_configuration(role, document[role])
        document.setdefault('queue', {'backend': 'storage'})
        identities = document['identities']
        _require(isinstance(identities, list) and 1 <= len(identities) <= MAX_IDENTITIES)
        seen = set()
        for identity in identities:
            key = 'token_sha256' if remote else 'uid'
            fields = (IDENTITY_FIELDS - {'uid'}) | {key}
            _require(isinstance(identity, dict) and set(identity) == fields)
            value = identity[key]
            _require((isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None)
                     if remote else (type(value) is int and value >= 0))
            _require(value not in seen)
            seen.add(value)
            identifier(identity['subject']); identifier(identity['organization'])
        resources, grants = document['resources'], document['grants']
        _require(isinstance(resources, list) and len(resources) <= 10000
                 and isinstance(grants, list) and len(grants) <= 1024)
        for resource in resources:
            _require(isinstance(resource, dict) and {'organization','kind','id'} <= set(resource)
                     and set(resource) <= RESOURCE_FIELDS and isinstance(resource.get('groups', []), list))
        for grant in grants:
            _require(isinstance(grant, dict) and {'subject','organization','rule_kind','operations','scopes'} <= set(grant)
                     and set(grant) <= GRANT_FIELDS)
            for key in ('operations', 'scopes', 'actions', 'providers'):
                _require(isinstance(grant.get(key, []), list))
            _require(all(isinstance(s, list) and len(s) == 2 for s in grant['scopes']))
        if 'observation_keys' in document:
            keys = document['observation_keys']
            _require(type(keys) is dict and 1 <= len(keys) <= 32)
            for key, key_path in keys.items():
                identifier(key)
                _require(isinstance(key_path, str) and 0 < len(key_path) <= 4096)
        if 'dispatch_bindings' in document:
            _require(isinstance(document['dispatch_bindings'], str) and 0 < len(document['dispatch_bindings']) <= 4096)
        if 'test_alert_delivery' in document:
            from .test_alert_delivery import validate_delivery
            validate_delivery(document['test_alert_delivery'])
        if 'execution_adapters' in document:
            from .execution_adapters import validate_adapters
            validate_adapters(document['execution_adapters'], resources)
        from .membership import configured_membership
        _require('membership' not in document or document['membership'] is not None)
        authority = configured_membership(document.get('membership'), path)
        policy = AccessPolicy([Grant(**dict(g, scopes=[tuple(s) for s in g['scopes']])) for g in grants],
                              [Resource(**r) for r in resources], membership=authority)
        return document, policy
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_rule_operator_configuration') from None


class LocalRuleOperator:
    _load = staticmethod(load_operator_config)

    def __init__(self, path):
        from .backend_registry import open_storage, destination
        self.path = Path(path).absolute()
        initial, _ = self._load(self.path)
        self._destination = destination(initial)
        self.actor()  # Authorize before creating or opening persistent storage.
        self.backend = open_storage(initial['storage'])
        try:
            self.client = self.backend.client
            from .rule_execution import RuleExecutionRuntime
            self.execution_runtime = RuleExecutionRuntime(self.backend, initial['namespace'], self._reload, self.path)
            self.application = build_rule_application(storage_backend=self.backend,
                                actor_loader=self.actor, policy_loader=lambda: self._reload()[1],
                                namespace=initial['namespace'],
                                dispatcher_factory=lambda lifecycle: self.execution_runtime.dispatcher(lifecycle, self.credential))
        except BaseException:
            self.backend.close()
            raise

    def _reload(self):
        document, policy = self._load(self.path)
        from .backend_registry import destination
        if destination(document) != self._destination:
            raise ConfigurationError('operator_destination_changed_restart_required')
        return document, policy

    def actor(self):
        document, policy = self._reload()
        matches = [identity for identity in document['identities'] if identity['uid'] == os.getuid()]
        if len(matches) != 1:
            raise PermissionError('rule_operator_not_mapped')
        actor = RuleActor(matches[0]['subject'], matches[0]['organization'])
        if not policy.member(actor.subject, actor.organization):
            raise PermissionError('account_membership_required')
        return actor

    def credential(self, principal):
        actor = self.actor()
        if (actor.subject, actor.organization) != (principal.subject, principal.organization):
            raise PermissionError('dispatch_identity_changed')
        return 'bearer:' + self.digest if hasattr(self, 'digest') else 'uid:' + str(os.getuid())

    def close(self):
        self.backend.close()
