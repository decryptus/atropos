"""Atropos owns its private policy, identities and storage configuration."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from atropos_client.config import ConfigurationError
from atropos.rule_operator import LocalRuleOperator, load_operator_config
from atropos.rule_server import load_service_config, RuleSession
from httpdis.authentication import Identity


class EngineConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'engine.yml'
        self.doc={'version':1,'namespace':'test','storage':{'backend':'redis','url':'redis://127.0.0.1:1/0'},
                  'identities':[{'uid':os.getuid(),'subject':'alice','organization':'org'}],
                  'resources':[],'grants':[]}
        self.write()

    def write(self):
        self.path.write_text(yaml.safe_dump(self.doc));self.path.chmod(0o600)

    def write_membership(self, members):
        import json
        path=self.root/'members.json'
        path.write_text(json.dumps({'membership':{'version':1,'revision':'a'*32,'members':members}}))
        path.chmod(0o600)

    def test_local_and_queued_identity_reload_current_membership(self):
        self.doc['membership']={'backend':'file','path':'members.json'}
        self.doc['observation_keys']={'key':'elsewhere/key.pem'}
        self.write();self.write_membership([dict(subject='alice',organization='org',groups=[])])
        operator=LocalRuleOperator(self.path);self.addCleanup(operator.close)
        actor=operator.actor()
        self.assertTrue(operator.execution_runtime.identity_valid(actor,'uid:'+str(os.getuid())))
        self.write_membership([])
        with self.assertRaises(PermissionError):operator.actor()
        self.assertFalse(operator.execution_runtime.identity_valid(actor,'uid:'+str(os.getuid())))

    def test_remote_credentials_cannot_bypass_membership(self):
        digest='a'*64
        self.doc['identities']=[dict(token_sha256=digest,subject='alice',organization='org')]
        self.doc['membership']={'backend':'file','path':'members.json'}
        self.write();self.write_membership([dict(subject='alice',organization='org',groups=[])])
        session=RuleSession(self.path,Identity(digest,'bearer'),({'backend':'redis','url':'redis://127.0.0.1:1/0'},{'backend':'storage'},'test'))
        self.addCleanup(session.close);self.assertEqual(session.actor().subject,'alice')
        self.write_membership([])
        with self.assertRaises(PermissionError):session.actor()
        with self.assertRaises(PermissionError):RuleSession(self.path,Identity(digest,'bearer'),({'backend':'redis','url':'redis://127.0.0.1:1/0'},{'backend':'storage'},'test'))

    def test_invalid_membership_configuration_and_group_grant_without_authority_rejected(self):
        for value in (None,{},dict(backend='redis',path='x')):
            self.doc['membership']=value;self.write()
            with self.assertRaises(ConfigurationError):load_operator_config(self.path)
        self.doc.pop('membership')
        self.doc['grants']=[dict(subject='ops',subject_kind='group',organization='org',rule_kind='intervention',
            operations=['read'],scopes=[['server','one']])];self.write()
        with self.assertRaises(ConfigurationError):load_operator_config(self.path)

    def test_private_regular_file_and_no_symlink(self):
        load_operator_config(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(ConfigurationError):load_operator_config(self.path)
        self.path.chmod(0o600);link=self.root/'link';link.symlink_to(self.path)
        with self.assertRaises(ConfigurationError):load_operator_config(link)

    def test_unknown_backend_ambiguous_destination_and_insecure_url_rejected(self):
        original=copy.deepcopy(self.doc)
        changes=[{'redis_url':original['storage']['url']},{'storage':{'backend':'other','url':'redis://localhost'}},
                 {'storage':{'backend':'redis','url':'redis://192.0.2.1'}},
                 {'storage':{'backend':'redis','url':'rediss://example.invalid?ssl_cert_reqs=none'}},
                 {'storage':{'backend':'redis'}},{'storage':None},{'identities':original['identities']*2},
                 {'version':True}]
        for change in changes:
            self.doc=dict(original,**change);self.write()
            with self.subTest(change=change),patch('redis.Redis.from_url') as client:
                with self.assertRaises(ConfigurationError):LocalRuleOperator(self.path)
                client.assert_not_called()

    def test_legacy_storage_spelling_rejected_and_destination_pinned(self):
        operator=LocalRuleOperator(self.path);self.addCleanup(operator.close)
        self.doc['redis_url']=self.doc.pop('storage')['url'];self.write()
        with self.assertRaises(ConfigurationError):load_operator_config(self.path)
        self.doc['storage']={'backend':'redis','url':self.doc.pop('redis_url')}
        self.doc['queue']={'backend':'redis','url':self.doc['storage']['url']};self.write()
        with self.assertRaises(ConfigurationError):operator.actor()

    def test_unmapped_uid_fails_before_storage_command(self):
        self.doc['identities'][0]['uid']+=1;self.write()
        with patch('redis.Redis.execute_command',side_effect=AssertionError('must not connect')):
            with self.assertRaises(PermissionError):LocalRuleOperator(self.path)

    def test_remote_identity_policy_reload_and_identity_change_denial(self):
        digest=hashlib.sha256(b'synthetic').hexdigest()
        self.doc['identities']=[{'token_sha256':digest,'subject':'alice','organization':'org'}];self.write()
        load_service_config(self.path)
        session=RuleSession(self.path,Identity(digest,'bearer'),({'backend':'redis','url':'redis://127.0.0.1:1/0'},{'backend':'storage'},'test'))
        self.addCleanup(session.close)
        self.assertEqual(session.actor().subject,'alice')
        self.doc['identities'][0]['subject']='bob';self.write()
        with self.assertRaises(PermissionError):session.actor()

    def test_policy_remains_engine_owned_and_atraxis_settings_are_rejected(self):
        self.doc['inventory_client']='atraxis.yml';self.write()
        with self.assertRaises(ConfigurationError):load_operator_config(self.path)

    def test_test_delivery_configuration_is_explicit_and_validated_offline(self):
        self.doc['test_alert_delivery']={'backend':'webhook','url':'https://receiver.example','token_file':'receiver.token'}
        self.write();load_operator_config(self.path)
        for change in ({'backend':'email'}, {'url':'http://receiver.example'}, {'token_file':''}, {'extra':True}):
            original=copy.deepcopy(self.doc)
            self.doc['test_alert_delivery'].update(change);self.write()
            with self.assertRaises(ConfigurationError):load_operator_config(self.path)
            self.doc=original

    def test_binding_listing_is_offline_and_invalid_yaml_never_leaks_content(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from atropos.cli import main
        self.doc['identities']=[{'token_sha256':'a'*64,'subject':'alice','organization':'org'}]
        self.doc['dispatch_bindings']='bindings.yml';self.write()
        path=self.root/'bindings.yml';path.write_text('[]');path.chmod(0o600)
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            self.assertEqual(main(['rules-bindings','--config',str(self.path)]),0)
        self.assertEqual(out.getvalue().strip(),'[]')  # Storage port 1 is unreachable.
        path.write_text('secret-synthetic-value: [unterminated')
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            self.assertEqual(main(['rules-bindings','--config',str(self.path)]),2)
        self.assertNotIn('secret-synthetic-value',out.getvalue()+err.getvalue())

    def test_sqlite_operator_uses_generic_backend_for_drafts_and_runtime(self):
        self.doc['storage']={'backend':'sqlite','url':'sqlite:///'+str(self.root/'engine.db')}
        self.doc['queue']={'backend':'storage'};self.write()
        operator=LocalRuleOperator(self.path);self.addCleanup(operator.close)
        self.assertEqual(operator.backend.__class__.__name__,'SQLiteBackend')
        self.assertIs(operator.execution_runtime.store.client,operator.backend.client)
        self.assertIs(operator.execution_runtime.test_store.client,operator.backend.client)
        self.doc['storage']['url']='sqlite:///'+str(self.root/'other.db');self.write()
        with self.assertRaises(ConfigurationError):operator.actor()
        self.assertFalse((self.root/'other.db').exists())
