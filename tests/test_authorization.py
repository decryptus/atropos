import unittest

from atropos.authorization import AccessPolicy, Grant, Resource


def inventory():
    return [Resource('org', 'server', 'node-a', groups=frozenset({'preprod'})),
            Resource('org', 'server', 'node-b', groups=frozenset({'prod'})),
            Resource('org', 'container', 'api-a', 'node-a', frozenset({'api'})),
            Resource('org', 'container', 'api-b', 'node-b', frozenset({'api'})),
            Resource('org', 'container', 'db-a', 'node-a', frozenset({'database'})),
            Resource('other', 'server', 'node-a', groups=frozenset({'preprod'}))]


class AuthorizationTests(unittest.TestCase):
    def policy(self, kind, scope, operations=('execute',), actions=('core.echo',), providers=()):
        grant = Grant('alice', 'org', kind, frozenset(operations), frozenset({scope}),
                      frozenset(actions), frozenset(providers))
        return AccessPolicy([grant], inventory())

    def allowed(self, policy, kind='container', target='api-a', **kwargs):
        return policy.allows(kwargs.get('subject', 'alice'), kwargs.get('organization', 'org'),
                             kwargs.get('rule_kind', 'intervention'), kwargs.get('operation', 'execute'),
                             [(kind, target)], actions=kwargs.get('actions', ('core.echo',)),
                             provider=kwargs.get('provider'))

    def test_container_scope_does_not_grant_whole_server(self):
        policy = self.policy('intervention', ('container', 'api-a'))
        self.assertTrue(self.allowed(policy))
        self.assertFalse(self.allowed(policy, 'container', 'db-a'))
        self.assertFalse(self.allowed(policy, 'server', 'node-a'))

    def test_server_scope_includes_its_containers(self):
        policy = self.policy('intervention', ('server', 'node-a'))
        self.assertTrue(self.allowed(policy, 'container', 'db-a'))
        self.assertTrue(self.allowed(policy, 'server', 'node-a'))
        self.assertFalse(self.allowed(policy, 'container', 'api-b'))

    def test_server_group_uses_trusted_membership(self):
        policy = self.policy('intervention', ('server_group', 'preprod'))
        self.assertTrue(self.allowed(policy))
        self.assertFalse(self.allowed(policy, 'container', 'api-b'))
        self.assertFalse(self.allowed(policy, 'container', 'unknown'))

    def test_container_group_can_span_servers_without_granting_servers(self):
        policy = self.policy('intervention', ('container_group', 'api'))
        self.assertTrue(self.allowed(policy, 'container', 'api-a'))
        self.assertTrue(self.allowed(policy, 'container', 'api-b'))
        self.assertFalse(self.allowed(policy, 'container', 'db-a'))
        self.assertFalse(self.allowed(policy, 'server', 'node-a'))

    def test_user_organization_kind_operation_and_action_are_independent(self):
        policy = self.policy('intervention', ('server_group', 'preprod'))
        for override in ({'subject': 'bob'}, {'organization': 'other'}, {'rule_kind': 'alerting'},
                         {'operation': 'activate'}, {'actions': ('core.fail',)}):
            with self.subTest(override=override):
                self.assertFalse(self.allowed(policy, **override))

    def test_edit_permission_does_not_grant_execution(self):
        policy = self.policy('supervision', ('server_group', 'preprod'), ('update', 'validate'), ())
        self.assertTrue(self.allowed(policy, rule_kind='supervision', operation='update', actions=()))
        self.assertFalse(self.allowed(policy))

    def test_export_permission_is_provider_scoped(self):
        policy = self.policy('alerting', ('server_group', 'preprod'), ('export',), (), ('github-backup',))
        self.assertTrue(self.allowed(policy, rule_kind='alerting', operation='export', actions=(), provider='github-backup'))
        self.assertFalse(self.allowed(policy, rule_kind='alerting', operation='export', actions=(), provider='s3-other'))
        self.assertFalse(self.allowed(policy, rule_kind='alerting', operation='export', actions=()))

    def test_all_requested_targets_must_be_authorized(self):
        policy = self.policy('intervention', ('server', 'node-a'))
        self.assertFalse(policy.allows('alice', 'org', 'intervention', 'execute',
                                      [('container', 'api-a'), ('container', 'api-b')], ['core.echo']))

    def test_unknown_parent_and_implicit_wildcards_rejected(self):
        with self.assertRaises(ValueError):
            AccessPolicy([], [Resource('org', 'container', 'orphan', 'missing')])
        with self.assertRaises(ValueError):
            self.policy('intervention', ('server', '*'))
