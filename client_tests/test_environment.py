"""Connection precedence and trust boundaries, without contacting external services."""
from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from atropos_client.client import RuleClient
from atropos_client.connection_config import load_remote_config
from atropos_client.config import ConfigurationError
from atropos_client.worker import fetch

TOKEN = 'a' * 43
OTHER_TOKEN = 'b' * 43


class EnvironmentConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.secret = self.root/'profile.token'
        self.secret.write_text(TOKEN); self.secret.chmod(0o600)
        self.profile = self.root/'client.yml'
        self.write_profile({'server': {'url': 'https://file.example'},
                            'token_file': 'profile.token', 'timeout': 12})

    def write_profile(self, data):
        self.profile.write_text(yaml.safe_dump(data)); self.profile.chmod(0o600)

    def test_environment_only_without_profile(self):
        config = load_remote_config(environ={'ATROPOS_SERVER_URL': 'https://env.example/',
            'ATROPOS_TOKEN': TOKEN, 'ATROPOS_TIMEOUT': '20'})
        self.assertEqual((config.origin, config.token, config.token_file, config.timeout),
                         ('https://env.example', TOKEN, None, 20))
        self.assertNotIn(TOKEN, repr(config))

    def test_explicit_environment_file_precedence(self):
        config = load_remote_config(self.profile, environ={
            'ATROPOS_SERVER_URL': 'https://env.example', 'ATROPOS_TOKEN': OTHER_TOKEN,
            'ATROPOS_TIMEOUT': '20'}, overrides={'origin': 'https://cli.example', 'timeout': 25})
        self.assertEqual((config.origin, config.timeout, config.token, config.token_file),
                         ('https://cli.example', 25, OTHER_TOKEN, None))
        config = load_remote_config(self.profile, environ={'ATROPOS_TOKEN': OTHER_TOKEN},
                                   overrides={'token_file': str(self.secret)})
        self.assertIsNone(config.token)
        self.assertEqual(config.token_file, str(self.secret))

    def test_profile_and_environment_paths_have_distinct_bases(self):
        cwd = self.root/'cwd'; cwd.mkdir()
        token = cwd/'environment.token'; token.write_text(OTHER_TOKEN); token.chmod(0o600)
        with patch('pathlib.Path.cwd', return_value=cwd):
            profile = load_remote_config(self.profile)
            config = load_remote_config(self.profile, environ={'ATROPOS_TOKEN_FILE': 'environment.token'})
        self.assertEqual(profile.token_file, str(self.secret))
        self.assertEqual(config.token_file, str(token))

    def test_prefixes_are_isolated_and_ambient_environment_is_opt_in(self):
        env = {'ATRAXIS_SERVER_URL':'https://gateway.example', 'ATRAXIS_TOKEN': OTHER_TOKEN,
               'ATROPOS_SERVER_URL':'https://untrusted.example', 'ATROPOS_TOKEN': OTHER_TOKEN}
        with patch.dict(os.environ, env, clear=True):
            server_owned = RuleClient(self.profile, expected_actor={'subject':'alice','organization':'org'})
            gateway = RuleClient(env_prefix='ATRAXIS', service='atraxis')
        self.assertEqual(server_owned.config.origin, 'https://file.example')
        self.assertEqual(server_owned.config.token_file, str(self.secret))
        self.assertEqual(gateway.config.origin, 'https://gateway.example')
        with self.assertRaises(ConfigurationError):load_remote_config(environ=env, prefix='UNKNOWN')

    def test_invalid_explicit_values_never_fall_back_or_echo_secrets(self):
        invalid = [{'ATROPOS_TOKEN': ''}, {'ATROPOS_TOKEN_FILE': ''},
                   {'ATROPOS_SERVER_URL': ''}, {'ATROPOS_CA_FILE': ''}]
        invalid += [{'ATROPOS_TIMEOUT': x} for x in ('', '0', '61', '-1', '1.5', ' 2', 'true')]
        invalid += [{'ATROPOS_SERVER_URL': x} for x in ('http://127.0.0.1',
                     'https://user:SECRET@host.example', 'https://host.example/path')]
        invalid += [{'ATROPOS_TOKEN': TOKEN, 'ATROPOS_TOKEN_FILE': str(self.secret)}]
        for env in invalid:
            with self.subTest(keys=list(env)), self.assertRaises(ConfigurationError) as caught:
                load_remote_config(self.profile, environ=env)
            self.assertEqual(str(caught.exception), 'invalid_rule_client_configuration')
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_partial_profile_and_required_final_configuration(self):
        self.write_profile({'timeout': 18})
        config = load_remote_config(self.profile, environ={
            'ATROPOS_SERVER_URL':'https://env.example','ATROPOS_TOKEN_FILE': str(self.secret)})
        self.assertEqual(config.timeout, 18)
        for env in ({}, {'ATROPOS_SERVER_URL':'https://env.example'}, {'ATROPOS_TOKEN':TOKEN}):
            with self.assertRaises(ConfigurationError):load_remote_config(environ=env)

    def test_environment_cannot_enable_http_or_change_server_owned_policy(self):
        env = {'ATROPOS_SERVER_URL':'http://127.0.0.1','ATROPOS_TOKEN':TOKEN,
               'ATROPOS_ALLOW_LOOPBACK_HTTP':'true'}
        with self.assertRaises(ConfigurationError):load_remote_config(environ=env)
        for field in ('actor','organization','storage','allow_loopback_http','token'):
            with self.assertRaises(ConfigurationError):
                load_remote_config(self.profile, overrides={field:'anything'})
        self.write_profile({'server':{'url':'https://file.example'},'token_file':'profile.token','actor':'admin'})
        with self.assertRaises(ConfigurationError):load_remote_config(self.profile)

    def test_worker_requires_one_credential_and_never_falls_back(self):
        config = load_remote_config(environ={'ATROPOS_SERVER_URL':'https://env.example','ATROPOS_TOKEN':TOKEN})
        for changes in ({'token_file':str(self.secret)}, {'token':None}, {'token':'invalid'}):
            doc = dict(asdict(config), request={'method':'catalog','arguments':{}}, **{})
            doc.update(changes)
            with patch('atropos_client.worker.http.client.HTTPSConnection') as transport:
                self.assertEqual(fetch(doc), {'error':'rule_service_unavailable'})
                transport.assert_not_called()

    def test_file_permissions_and_shadowed_ambiguity_remain_errors(self):
        self.secret.chmod(0o644)
        with self.assertRaises(ConfigurationError):load_remote_config(self.profile)
        self.secret.chmod(0o600)
        self.profile.chmod(0o644)
        with self.assertRaises(ConfigurationError):load_remote_config(self.profile)
        self.profile.chmod(0o600)
        with self.assertRaises(ConfigurationError):
            load_remote_config(self.profile, environ={'ATROPOS_TOKEN':TOKEN,'ATROPOS_TOKEN_FILE':str(self.secret)},
                               overrides={'token_file':str(self.secret)})

    def test_organization_selection_is_only_for_atraxis_and_cannot_inject_headers(self):
        for service,organization in (('atropos','one'),('atraxis','one\r\nInjected: yes'),('atraxis',''),('atraxis',12)):
            with self.assertRaises(ValueError):RuleClient(self.profile,service=service,organization=organization)
        client=RuleClient(self.profile,service='atraxis',organization='one')
        self.assertEqual(client.organization,'one')
        self.assertIsNone(client.expected_actor)
