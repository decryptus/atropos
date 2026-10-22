import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy

from atropos.authorization import AccessPolicy, Grant, Resource
from atropos.membership import FileMembershipAuthority, configured_membership, decode_membership


def envelope(groups=('ops',)):
    return {'membership': {'version':1,'revision':'a'*32,'members':[
        {'subject':'alice','organization':'org','groups':list(groups)}]}}


class MembershipTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.path=self.root/'membership.json'
        self.write(envelope());self.authority=FileMembershipAuthority(self.path)
        self.resources=[Resource('org','server','one'),Resource('org','server','two')]

    def write(self, data):
        temporary=self.root/'next.json';temporary.write_text(json.dumps(data));temporary.chmod(0o600)
        temporary.replace(self.path)

    def grant(self, **changes):
        data=dict(subject='ops',subject_kind='group',organization='org',rule_kind='intervention',
                  operations=['execute'],scopes=[('server','one')],actions=['core.echo'])
        data.update(changes);return Grant(**data)

    def allowed(self, policy, **changes):
        data=dict(subject='alice',organization='org',rule_kind='intervention',operation='execute',
                  targets=[('server','one')],actions=['core.echo'])
        data.update(changes);return policy.allows(**data)

    def test_current_membership_and_groups_reloaded_without_cached_allow(self):
        self.assertEqual(self.authority.groups('alice','org'),frozenset({'ops'}))
        self.write(envelope([]));self.assertEqual(self.authority.groups('alice','org'),frozenset())
        data=envelope();data['membership']['members']=[];self.write(data)
        self.assertIsNone(self.authority.groups('alice','org'))
        self.assertIsNone(self.authority.groups('alice','other'))

    def test_group_grants_keep_domain_action_resource_and_org_bounds(self):
        policy=AccessPolicy([self.grant()],self.resources,membership=self.authority)
        self.assertTrue(self.allowed(policy))
        for change in ({'organization':'other'},{'actions':['core.fail']},{'operation':'read'},
                       {'rule_kind':'alerting'},{'targets':[('server','two')]}):
            self.assertFalse(self.allowed(policy,**change))
        self.write(envelope([]));self.assertFalse(self.allowed(policy))

    def test_user_and_group_names_do_not_alias(self):
        policy=AccessPolicy([self.grant(subject_kind='user')],self.resources,membership=self.authority)
        self.assertFalse(self.allowed(policy))
        self.assertFalse(self.allowed(policy,subject='ops'))
        with self.assertRaises(ValueError):AccessPolicy([self.grant()],self.resources)

    def test_user_grants_also_require_membership(self):
        policy=AccessPolicy([self.grant(subject='alice',subject_kind='user')],self.resources,membership=self.authority)
        self.assertTrue(self.allowed(policy))
        data=envelope();data['membership']['members']=[];self.write(data)
        self.assertFalse(self.allowed(policy));self.assertFalse(policy.member('alice','org'))

    def test_groups_cannot_union_partial_target_or_action_grants(self):
        policy=AccessPolicy([self.grant(),self.grant(scopes=[('server','two')],actions=['core.fail'])],
                            self.resources,membership=self.authority)
        self.assertFalse(self.allowed(policy,targets=[('server','one'),('server','two')]))
        self.assertFalse(self.allowed(policy,actions=['core.echo','core.fail']))

    def test_missing_malformed_public_symlink_fail_closed(self):
        self.path.unlink()
        with self.assertRaises(PermissionError):self.authority.groups('alice','org')
        self.write(envelope());self.path.chmod(0o644)
        with self.assertRaises(PermissionError):self.authority.groups('alice','org')
        self.path.chmod(0o600);target=self.root/'target';self.path.rename(target);self.path.symlink_to(target)
        with self.assertRaises(PermissionError):self.authority.groups('alice','org')
        self.path.unlink();self.path.write_text('{"secret":"private"}');self.path.chmod(0o600)
        with self.assertRaisesRegex(PermissionError,'^membership_authority_unavailable$'):
            self.authority.groups('alice','org')

    def test_strict_wire_version_duplicates_and_types(self):
        changes=[lambda d:d['membership'].update(version=True),lambda d:d['membership'].update(revision='x'),
            lambda d:d['membership'].update(extra=1),lambda d:d['membership']['members'].append(d['membership']['members'][0]),
            lambda d:d['membership']['members'][0].update(groups=['ops','ops']),
            lambda d:d['membership']['members'][0].update(groups='ops'),
            lambda d:d['membership']['members'][0].update(subject='*')]
        for change in changes:
            data=envelope();change(data)
            with self.assertRaises(ValueError):decode_membership(json.dumps(data).encode())
        with self.assertRaises(ValueError):decode_membership(b'{"membership":{},"membership":{}}')

    def test_owner_envelope_is_not_interpreted_and_empty_view_denies_all(self):
        data=envelope();data['unrelated_owner_data']={'format':999}
        self.assertIn(('alice','org'),decode_membership(json.dumps(data).encode())[1])
        data['membership']['members']=[]
        self.assertEqual(decode_membership(json.dumps(data).encode())[1],{})

    def test_configuration_is_generic_explicit_and_relative_to_profile(self):
        self.assertEqual(configured_membership({'backend':'file','path':'membership.json'},self.root/'service.yml').path,self.path)
        for spec in ({'backend':'redis','path':'x'},{'backend':'file','path':''},{'backend':'file','path':'x','url':'x'}):
            with self.assertRaises(ValueError):configured_membership(spec,self.root/'service.yml')
