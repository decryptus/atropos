"""Scoped draft/test/active lifecycle. No network transport or action execution.

The caller supplies an authenticated principal and a current trusted policy
loader. The backend remains trusted storage; direct access bypasses this boundary.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass

from .authorization import AccessPolicy, RULE_KINDS, SCOPE_KINDS, identifier
from .definitions import encode, name
from .storage_contract import Conflict
from .rule_model import RuleError, dump_rules, parse_rules, validate_rules
from .zen_rules import COMPILER_VERSION, ZenRules

MAX_RETIREMENT_REVISIONS = 128
MAX_TARGETS = 128
MAX_CASES = 100
MAX_CASE_BYTES = 65536
INTERNAL_PROVIDER = 'internal'
CONTEXT_FIELDS = frozenset({'version', 'rule_kind', 'targets'})
CASE_FIELDS = frozenset({'name', 'input', 'expected'})
RESULT_FIELDS = frozenset({'decision', 'matched_rule'})


def _digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _revision(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid_revision')
    return value


def _page(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_history_page')


@dataclass(frozen=True)
class Principal:
    subject: str
    organization: str

    def __post_init__(self):
        identifier(self.subject)
        identifier(self.organization)


class RuleLifecycle:
    def __init__(self, repository, state_store, policy_loader):
        self.repository = repository
        self.store = state_store
        self.policy_loader = policy_loader

    def _policy(self, actor):
        if not isinstance(actor, Principal) or actor.organization != self.repository.organization:
            raise PermissionError('rule_access_denied')
        policy = self.policy_loader()
        if not isinstance(policy, AccessPolicy):
            raise PermissionError('policy_unavailable')
        return policy

    def _authorize(self, policy, actor, context, operation, provider=None):
        if (not isinstance(context, dict) or set(context) != CONTEXT_FIELDS
                or type(context['version']) is not int or context['version'] != 1
                or context['rule_kind'] not in RULE_KINDS
                or not isinstance(context['targets'], list)
                or not 1 <= len(context['targets']) <= MAX_TARGETS):
            raise ValueError('invalid_rule_context')
        targets = context['targets']
        if any(not isinstance(t, list) or len(t) != 2 or any(not isinstance(v, str) for v in t) for t in targets):
            raise ValueError('invalid_rule_context')
        if not policy.allows(actor.subject, actor.organization, context['rule_kind'], operation,
                             targets, provider=provider):
            raise PermissionError('rule_access_denied')

    def _context(self, policy, actor, rule_kind, selections):
        if rule_kind not in RULE_KINDS or not isinstance(selections, (list, tuple)) or not 1 <= len(selections) <= MAX_TARGETS:
            raise ValueError('invalid_rule_scope')
        targets = set()
        for selection in selections:
            if not isinstance(selection, (list, tuple)) or len(selection) != 2:
                raise ValueError('invalid_rule_scope')
            kind, value = selection
            if not isinstance(kind, str) or kind not in SCOPE_KINDS:
                raise ValueError('invalid_rule_scope')
            identifier(value)
            if kind in ('server', 'container'):
                resource = policy.resource(actor.organization, kind, value)
                found = [resource] if resource else []
            else:
                resource_kind = 'server' if kind == 'server_group' else 'container'
                found = [r for r in policy.inventory.values() if r.organization == actor.organization
                         and r.kind == resource_kind and value in r.groups]
            if not found:
                raise ValueError('empty_or_unknown_rule_scope')
            targets.update((r.kind, r.id) for r in found)
            if len(targets) > MAX_TARGETS:
                raise ValueError('rule_target_limit_exceeded')
        return {'version': 1, 'rule_kind': rule_kind, 'targets': [list(t) for t in sorted(targets)]}

    def _get(self, policy, actor, rule_name, revision, operation):
        record = self.repository.get(rule_name, revision)
        self._authorize(policy, actor, record.get('context'), operation)
        return record

    def save_draft(self, actor, document, *, rule_kind, selections, expected_revision, message=''):
        policy = self._policy(actor)
        document = validate_rules(document)
        context = self._context(policy, actor, rule_kind, selections)
        operation = 'create' if expected_revision is None else 'update'
        if expected_revision is not None:
            previous = self._get(policy, actor, document['name'], expected_revision, 'update')
            self._authorize(policy, actor, previous['context'], 'save', INTERNAL_PROVIDER)
        self._authorize(policy, actor, context, operation)
        self._authorize(policy, actor, context, 'save', INTERNAL_PROVIDER)
        return self.repository.save(document, actor.subject, expected_revision, message, context=context)

    def read(self, actor, rule_name, revision):
        return self._get(self._policy(actor), actor, rule_name, revision, 'read')

    def draft(self, actor, rule_name):
        policy = self._policy(actor)
        revision = self.repository.head(rule_name)
        if revision is None:
            raise KeyError('rule_not_found')
        return self._get(policy, actor, rule_name, revision, 'read')

    def export_yaml(self, actor, rule_name, revision):
        policy = self._policy(actor)
        record = self._get(policy, actor, rule_name, revision, 'read')
        self._authorize(policy, actor, record['context'], 'export', 'download')
        return dump_rules(record['document'])

    def import_draft(self, actor, content, *, format='yaml', rule_kind, selections,
                     expected_revision, message=''):
        policy = self._policy(actor)
        context = self._context(policy, actor, rule_kind, selections)
        self._authorize(policy, actor, context, 'import', 'upload')
        return self.save_draft(actor, parse_rules(content, format), rule_kind=rule_kind,
                               selections=selections, expected_revision=expected_revision,
                               message=message)

    def history(self, actor, rule_name, offset=0, limit=50):
        policy = self._policy(actor)
        _page(offset, limit)
        result = []
        for item in self.repository.history(rule_name, offset, limit):
            try:
                self._authorize(policy, actor, item.get('context'), 'read')
            except PermissionError:
                continue
            result.append(item)
        return result

    def test(self, actor, rule_name, revision, cases):
        self.repository.require_available(rule_name)
        policy = self._policy(actor)
        record = self._get(policy, actor, rule_name, revision, 'test')
        # Copy before evaluation so caller mutation cannot change evidence.
        cases = json.loads(encode(cases, MAX_CASE_BYTES))
        if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
            raise RuleError('Expected 1–100 scenarios')
        for case in cases:
            if (not isinstance(case, dict) or set(case) != CASE_FIELDS
                    or not isinstance(case['name'], str) or not 1 <= len(case['name']) <= 128
                    or not isinstance(case['expected'], dict) or set(case['expected']) != RESULT_FIELDS
                    or any(not isinstance(v, str) for v in case['expected'].values())):
                raise RuleError('Invalid scenario')
        engine = ZenRules(record['document'])
        results = []
        for case in cases:
            try:
                actual = engine.evaluate(case['input'])
                results.append({'name': case['name'], 'passed': actual == case['expected'], 'actual': actual})
            except RuleError:
                results.append({'name': case['name'], 'passed': False, 'error': 'invalid_input_or_evaluation'})
        self._authorize(self._policy(actor), actor, record['context'], 'test')
        report = {'id': uuid.uuid4().hex, 'revision': revision, 'author': actor.subject,
                  'created_at': self.store.now(), 'content_sha256': record['content_sha256'],
                  'context_sha256': record['context_sha256'], 'compiler': COMPILER_VERSION,
                  'cases_sha256': _digest(cases), 'cases': cases, 'results': results,
                  'passed': all(item['passed'] for item in results)}
        self.store.save_test(rule_name, report)
        return report

    def test_report(self, actor, rule_name, revision, report_id):
        self._get(self._policy(actor), actor, rule_name, revision, 'read')
        report = self._report(rule_name, report_id)
        if report['revision'] != revision:
            raise ValueError('test_revision_mismatch')
        return report

    def _report(self, rule_name, report_id):
        return self.store.report(rule_name, _revision(report_id))

    def active(self, actor, rule_name):
        policy = self._policy(actor)
        state = self.store.state(rule_name)
        # A deactivated marker still belongs to the prior scope, never becomes public.
        if state.get('last_revision'):
            self._get(policy, actor, rule_name, state['last_revision'], 'read')
        else:
            head = self.repository.head(rule_name)
            if head is None:
                raise KeyError('rule_not_found')
            self._get(policy, actor, rule_name, head, 'read')
        return state

    def activate(self, actor, rule_name, revision, *, test_id, expected_generation):
        return self._change(actor, rule_name, revision, test_id, expected_generation, 'activate')

    def rollback(self, actor, rule_name, revision, *, test_id, expected_generation):
        return self._change(actor, rule_name, revision, test_id, expected_generation, 'rollback')

    def deactivate(self, actor, rule_name, *, expected_generation):
        return self._change(actor, rule_name, None, None, expected_generation, 'deactivate')

    def _change(self, actor, rule_name, revision, test_id, expected_generation, operation):
        name(rule_name, 'rule name')
        if expected_generation is not None:
            _revision(expected_generation)
        state = self.store.state(rule_name)
        if state['generation'] != expected_generation:
            raise Conflict('active_generation_changed')
        policy = self._policy(actor)
        current = state['revision']
        old_revision = state.get('last_revision')
        if old_revision:
            self._get(policy, actor, rule_name, old_revision, operation)
            if operation == 'rollback':
                self._get(policy, actor, rule_name, old_revision, 'activate')
        if operation == 'deactivate':
            if current is None:
                raise Conflict('rule_not_active')
        else:
            record = self._get(policy, actor, rule_name, revision, operation)
            if operation == 'rollback':
                self._authorize(policy, actor, record['context'], 'activate')
                if not self.store.was_active(rule_name, revision):
                    raise ValueError('revision_was_never_active')
            if self.store.latest_test(rule_name, revision) != test_id:
                raise ValueError('latest_test_required')
            report = self._report(rule_name, test_id)
            if (report['revision'] != revision or report['passed'] is not True
                    or report['content_sha256'] != record['content_sha256']
                    or report['context_sha256'] != record['context_sha256']
                    or report['compiler'] != COMPILER_VERSION):
                raise ValueError('passing_test_required')
        return self.store.transition(rule_name, revision=revision, test_id=test_id,
                                     expected_generation=expected_generation,
                                     operation=operation, actor=actor)

    def audit(self, actor, rule_name, offset=0, limit=50):
        policy = self._policy(actor)
        _page(offset, limit)
        events = self.store.audit(rule_name, offset, limit)
        visible = []
        for event in events:
            try:
                disposition = self.repository.availability(rule_name)
                for revision in event['scope_revisions']:
                    if disposition['status'] == 'deleted':
                        self._authorize(policy, actor, disposition['contexts'][revision], 'read')
                    else:
                        self._get(policy, actor, rule_name, revision, 'read')
            except PermissionError:
                continue
            visible.append(event)
        return visible

    def evaluate_active(self, actor, rule_name, target, facts):
        policy = self._policy(actor)
        state = self.store.state(rule_name)
        if state['revision'] is None:
            raise Conflict('rule_not_active')
        record = self._get(policy, actor, rule_name, state['revision'], 'evaluate')
        if not isinstance(target, (list, tuple)) or list(target) not in record['context']['targets']:
            raise PermissionError('target_outside_rule_scope')
        result = ZenRules(record['document']).evaluate(facts)
        # Discard a decision if activation changed while ZEN was evaluating.
        if self.store.state(rule_name)['generation'] != state['generation']:
            raise Conflict('active_generation_changed')
        self._authorize(self._policy(actor), actor, record['context'], 'evaluate')
        return {'revision': state['revision'], 'generation': state['generation'],
                'target': list(target), 'result': result}

    def availability(self, actor, rule_name):
        policy = self._policy(actor)
        value = self.repository.availability(rule_name)
        if value['status'] == 'deleted':
            for context in value['contexts'].values():
                self._authorize(policy, actor, context, 'read')
        else:
            self.draft(actor, rule_name)
        return dict({k: v for k, v in value.items() if k != 'contexts'}, name=rule_name)

    def archive(self, actor, rule_name, *, expected_revision, expected_generation):
        return self._retire(actor, rule_name, 'archive', expected_revision, expected_generation)

    def unarchive(self, actor, rule_name, *, expected_revision, expected_generation):
        return self._retire(actor, rule_name, 'unarchive', expected_revision, expected_generation)

    def delete(self, actor, rule_name, *, expected_revision, expected_generation):
        return self._retire(actor, rule_name, 'delete', expected_revision, expected_generation)

    def _retire(self, actor, rule_name, operation, revision, generation):
        _revision(revision)
        if generation is not None: _revision(generation)
        policy = self._policy(actor)
        contexts = {}
        offset = 0
        while True:
            rows = self.repository.history(rule_name, offset, 100)
            for record in rows:
                self._authorize(policy, actor, record.get('context'), operation)
                contexts[record['revision']] = record['context']
            offset += len(rows)
            if offset > MAX_RETIREMENT_REVISIONS: raise Conflict('rule_retirement_limit_exceeded')
            if len(rows) < 100: break
        if not contexts: raise KeyError('rule_not_found')
        return self.store.retire(rule_name, operation=operation, actor=actor,
                                 expected_revision=revision, expected_generation=generation,
                                 contexts=contexts)
