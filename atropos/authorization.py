"""Pure deny-by-default policy prototype, enforced by the Atropos rule service.

Only trusted server configuration supplies grants and inventory. Request bodies
must never supply their own group membership or permission set.
"""
import re
from dataclasses import dataclass

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
RULE_KINDS = frozenset({'supervision', 'intervention', 'alerting'})
OPERATIONS = frozenset({'read', 'create', 'update', 'validate', 'test', 'import', 'export',
                        'archive', 'unarchive', 'delete', 'save', 'activate', 'deactivate', 'execute', 'reconcile', 'rollback', 'evaluate'})
RESOURCE_KINDS = frozenset({'server', 'container'})
SCOPE_KINDS = RESOURCE_KINDS | {'server_group', 'container_group'}
MAX_GRANTS = 1024
MAX_RESOURCES = 10000
PROVIDER_OPERATIONS = frozenset({'import', 'export', 'save'})


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('invalid_identifier')
    return value


@dataclass(frozen=True)
class Resource:
    organization: str
    kind: str
    id: str
    server_id: str | None = None
    groups: frozenset = frozenset()

    def __post_init__(self):
        identifier(self.organization)
        identifier(self.id)
        if self.kind not in RESOURCE_KINDS:
            raise ValueError('invalid_resource_kind')
        if self.kind == 'container':
            identifier(self.server_id)
        elif self.server_id is not None:
            raise ValueError('server_cannot_have_parent')
        object.__setattr__(self, 'groups', frozenset(identifier(g) for g in self.groups))

    @property
    def execution_target(self):
        # The engine receives a canonical target, not a caller-selected lock name.
        value = f'{self.organization}/{self.kind}/{self.id}'
        if len(value) > 256:
            raise ValueError('resource_identity_too_long')
        return value


@dataclass(frozen=True)
class Grant:
    subject: str
    organization: str
    rule_kind: str
    operations: frozenset
    scopes: frozenset
    actions: frozenset = frozenset()
    providers: frozenset = frozenset()
    subject_kind: str = 'user'

    def __post_init__(self):
        identifier(self.subject)
        if self.subject_kind not in ('user', 'group'):
            raise ValueError('invalid_subject_kind')
        identifier(self.organization)
        if self.rule_kind not in RULE_KINDS:
            raise ValueError('invalid_rule_kind')
        operations = frozenset(self.operations)
        if not operations or not operations <= OPERATIONS:
            raise ValueError('invalid_operations')
        scopes = frozenset(self.scopes)
        if not scopes:
            raise ValueError('empty_scope')
        for kind, value in scopes:
            if kind not in SCOPE_KINDS:
                raise ValueError('invalid_scope_kind')
            identifier(value)
        object.__setattr__(self, 'operations', operations)
        object.__setattr__(self, 'scopes', scopes)
        object.__setattr__(self, 'actions', frozenset(identifier(a) for a in self.actions))
        object.__setattr__(self, 'providers', frozenset(identifier(p) for p in self.providers))


class AccessPolicy:
    def __init__(self, grants, inventory, *, membership=None):
        self.membership = membership
        self.grants = tuple(grants)
        resources = tuple(inventory)
        if len(self.grants) > MAX_GRANTS or len(resources) > MAX_RESOURCES:
            raise ValueError('policy_capacity_exceeded')
        if any(not isinstance(g, Grant) for g in self.grants):
            raise ValueError('invalid_grant')
        if membership is None and any(g.subject_kind == 'group' for g in self.grants):
            raise ValueError('group_grant_requires_membership_authority')
        self.inventory = {}
        for resource in resources:
            if not isinstance(resource, Resource):
                raise ValueError('invalid_resource')
            key = resource.organization, resource.kind, resource.id
            if key in self.inventory:
                raise ValueError('duplicate_resource')
            self.inventory[key] = resource
        for resource in resources:
            if resource.kind == 'container' and (
                    resource.organization, 'server', resource.server_id) not in self.inventory:
                raise ValueError('unknown_parent_server')

    def resource(self, organization, kind, value):
        return self.inventory.get((organization, kind, value))

    def _scopes(self, resource):
        scopes = {(resource.kind, resource.id)}
        if resource.kind == 'container':
            scopes.update(('container_group', group) for group in resource.groups)
        server = resource if resource.kind == 'server' else self.inventory[
            resource.organization, 'server', resource.server_id]
        scopes.add(('server', server.id))
        scopes.update(('server_group', group) for group in server.groups)
        return scopes

    def member(self, subject, organization):
        return self.membership is None or self.membership.groups(subject, organization) is not None

    def allows(self, subject, organization, rule_kind, operation, targets, actions=(), provider=None):
        groups = frozenset() if self.membership is None else self.membership.groups(subject, organization)
        if groups is None:
            return False
        if rule_kind not in RULE_KINDS or operation not in OPERATIONS:
            return False
        if operation in PROVIDER_OPERATIONS and provider is None:
            return False
        targets, actions = tuple(targets), frozenset(actions)
        if not targets or (operation == 'execute' and not actions):
            return False
        resources = [self.resource(organization, kind, value) for kind, value in targets]
        if any(resource is None for resource in resources):
            return False
        for grant in self.grants:
            matches = grant.subject == subject if grant.subject_kind == 'user' else grant.subject in groups
            if (not matches or grant.organization != organization
                    or grant.rule_kind != rule_kind or operation not in grant.operations
                    or not actions <= grant.actions
                    or (provider is not None and provider not in grant.providers)):
                continue
            if all(self._scopes(resource) & grant.scopes for resource in resources):
                return True
        return False
