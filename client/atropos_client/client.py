"""Terminal client for rule control; credentials never enter command arguments."""
from dataclasses import asdict, replace
import json
import os
import re
import subprocess
import sys

from .connection_config import RemoteConfig, load_remote_config, read_token
from .protocol import (METHODS, MUTATIONS, ERROR_CODES,
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, arguments, decode)


class RemoteRuleError(ValueError):
    """Fixed wire code only; response bodies and credentials are never propagated."""


class RuleClient:
    def __init__(self, path=None, *, expected_actor=None, service='atropos',
                 env_prefix=None, overrides=None, organization=None):
        if service not in ('atraxis', 'atropos'):
            raise ValueError('invalid_rule_service')
        if organization is not None and (service!='atraxis' or type(organization) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',organization) is None):
            raise ValueError('invalid_organization_context')
        self.organization=organization
        self.config = replace(load_remote_config(path,
            environ=os.environ if env_prefix is not None else None,
            prefix=env_prefix if env_prefix is not None else 'ATROPOS',
            overrides=overrides), service=service)
        self.expected_actor = expected_actor
        self.application = self
        self.live = self

    def __getattr__(self, method):
        if method not in METHODS:
            raise AttributeError(method)
        def call(*args, **kwargs):
            return self._call(method, arguments(method, *args, **kwargs))
        return call

    def evaluate(self, name, target):
        return self._call('observe', arguments('observe', name, target))

    def _call(self, method, values):
        request = {'method': method, 'arguments': values}
        if self.expected_actor is not None:
            request['expected_actor'] = dict(self.expected_actor)
        raw = json.dumps(request, ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > MAX_REQUEST_BYTES:
            raise RemoteRuleError('invalid_rule_request')
        document = dict(asdict(self.config), request=request)
        if self.organization is not None: document['organization']=self.organization
        try:
            completed = subprocess.run([sys.executable, '-m', 'atropos_client.worker'],
                input=json.dumps(document).encode(), capture_output=True,
                timeout=self.config.timeout, check=False,
                env={key: value for key, value in os.environ.items()
                     if key not in ('ATRAXIS_TOKEN', 'ATROPOS_TOKEN')})
            if completed.returncode or len(completed.stdout) > MAX_RESPONSE_BYTES:
                raise ValueError('invalid_worker_result')
            envelope = decode(completed.stdout)
            if type(envelope) is not dict:
                raise ValueError('invalid_worker_result')
            if set(envelope) == {'result'}:
                return envelope['result']
            if set(envelope) != {'error'} or envelope['error'] not in ERROR_CODES:
                raise ValueError('invalid_worker_result')
        except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
            code = 'rule_outcome_unknown' if method in MUTATIONS else 'rule_service_unavailable'
            raise RemoteRuleError(code) from None
        if envelope['error'] == 'rule_access_denied':
            raise PermissionError('rule_access_denied')
        raise RemoteRuleError(envelope['error'])

    def close(self):
        pass
