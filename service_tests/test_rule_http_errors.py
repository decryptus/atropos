"""Public command responses for invisible rules and unexpected backend errors."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from httpdis.authentication import Identity

from atropos.rule_server import build_context
from atropos_client.protocol import ROUTE


class RuleHttpErrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / 'service.yml'
        grants = [dict(subject='owner', organization='org', rule_kind='intervention',
                       operations=['read', 'save', 'create', 'deactivate'],
                       scopes=[['server', 'host']], providers=['internal'])]
        document = dict(version=1, namespace='test',
                        storage=dict(backend='sqlite', url='sqlite:///' + str(root / 'state.db')),
                        identities=[dict(subject=user, organization='org', token_sha256=digest)
                                    for user, digest in [('owner', 'a'*64), ('other', 'b'*64)]],
                        resources=[dict(organization='org', kind='server', id='host')], grants=grants)
        path.write_text(yaml.safe_dump(document))
        path.chmod(0o600)
        self.handle = build_context(path).commands['POST ' + ROUTE].handler

    def command(self, method, digest, **arguments):
        request = Mock()
        request.get_server_vars.return_value = {'HTTP_AUTH_IDENTITY': Identity(digest, 'bearer')}
        request.get_payload.return_value.getvalue.return_value = json.dumps(
            dict(method=method, arguments=arguments)).encode()
        response = self.handle(request)
        return response.code, json.loads(response.data)

    def test_hidden_draft_and_generation_are_indistinguishable_from_absence(self):
        status, _ = self.command('create_from_templates', 'a'*64, templates=['cpu-high'],
                                 name='cpu', rule_kind='intervention', selections=[['server', 'host']])
        self.assertEqual(status, 200)
        for method, args in [('draft', {}), ('deactivate', dict(expected_generation='0'*32))]:
            for name in ('cpu', 'absent'):
                with self.subTest(method=method, name=name):
                    self.assertEqual(self.command(method, 'b'*64, name=name, **args),
                                     (404, {'error': 'rule_not_found'}))
        self.assertEqual(self.command('deactivate', 'a'*64, name='cpu', expected_generation='0'*32)[0], 409)
        self.assertEqual(self.command('draft', 'a'*64, name='cpu')[0], 200)

    def test_internal_key_error_reports_unknown_outcome(self):
        with patch('atropos.rule_application.RuleApplication.catalog', side_effect=KeyError('internal')):
            self.assertEqual(self.command('catalog', 'a'*64), (503, {'error': 'rule_outcome_unknown'}))
