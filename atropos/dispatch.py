"""Explicit decision-to-workflow admission and execution-time authorization.

Only server-approved bindings choose workflows and inputs. Caller facts are used
for explicit evaluation, never copied into executable parameters or destinations.
"""
import copy
import hashlib
import json
import re

from .authorization import identifier
from .definitions import encode, name
from .exchange import ApprovedWorkflows
from .storage_contract import Conflict
from .rule_lifecycle import Principal, _revision

BINDING_FIELDS = frozenset({'organization', 'rule', 'revision', 'result', 'workflow', 'input', 'max_age_seconds'})
PROOF_FIELDS = frozenset({'version', 'binding', 'rule', 'revision', 'generation', 'result', 'facts_sha256', 'event_id', 'credential_id'})
HASH = re.compile(r'[0-9a-f]{64}')
MAX_BINDINGS = 64
MAX_FACT_BYTES = 65536
ISSUER = 'atropos.rules'


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def event_key(actor, rule, event_id):
    name(rule, 'rule'); identifier(event_id)
    return digest({'subject': actor.subject, 'organization': actor.organization, 'rule': rule, 'event': event_id})


class ApprovedBindings:
    """Immutable content-addressed records, supplied only by trusted configuration."""
    def __init__(self, documents, actions):
        if not isinstance(documents, list) or len(documents) > MAX_BINDINGS:
            raise ValueError('invalid_dispatch_bindings')
        self.records = {}
        for value in documents:
            document = json.loads(encode(value))
            if (not isinstance(document, dict) or not BINDING_FIELDS <= set(document)
                    or not set(document) <= BINDING_FIELDS | {'observation', 'adapter_revisions'}):
                raise ValueError('invalid_dispatch_binding')
            identifier(document['organization']); name(document['rule'], 'rule')
            _revision(document['revision'])
            result = document['result']
            if (not isinstance(result, dict) or set(result) != {'decision', 'matched_rule'}
                    or not all(isinstance(v, str) and 1 <= len(v) <= 128 for v in result.values())
                    or not isinstance(document['input'], dict)
                    or type(document['max_age_seconds']) is not int
                    or not 1 <= document['max_age_seconds'] <= 300):
                raise ValueError('invalid_dispatch_binding')
            if 'observation' in document:
                from .observation_authority import validate_spec
                validate_spec(document['observation'])
            catalog = ApprovedWorkflows([document['workflow']], actions)
            required_revisions = {step['action']: actions[step['action']].configuration_revision
                for step in document['workflow']['steps'].values()
                if actions[step['action']].configuration_revision is not None}
            if (document.get('adapter_revisions', {}) != required_revisions
                    or ('adapter_revisions' in document and not required_revisions)):
                raise ValueError('execution_adapter_revision_mismatch')
            revision = catalog.revision(document['workflow'])
            document['workflow'] = catalog.get(document['workflow']['name'], revision)
            self.records[digest(document)] = document

    def get(self, binding):
        if not isinstance(binding, str) or not HASH.fullmatch(binding):
            raise ValueError('invalid_binding_id')
        if binding not in self.records:
            raise PermissionError('dispatch_binding_not_approved')
        return copy.deepcopy(self.records[binding])


def authorize(lifecycle, actor, binding, target):
    record = lifecycle.read(actor, binding['rule'], binding['revision'])
    if (binding['organization'] != actor.organization
            or record['context']['rule_kind'] != 'intervention'
            or list(target) not in record['context']['targets']):
        raise PermissionError('dispatch_scope_denied')
    policy = lifecycle._policy(actor)
    lifecycle._authorize(policy, actor, record['context'], 'evaluate')
    actions = {step['action'] for step in binding['workflow']['steps'].values()}
    if not policy.allows(actor.subject, actor.organization, 'intervention', 'execute', [target], actions=actions):
        raise PermissionError('dispatch_action_denied')
    return policy.resource(actor.organization, *target)


class DecisionDispatcher:
    def __init__(self, lifecycle, workflow_service, bindings_loader, identity_loader):
        if workflow_service.store.client is not lifecycle.store.client:
            raise ValueError('dispatch_requires_shared_transaction_store')
        self.lifecycle, self.workflows = lifecycle, workflow_service
        self.bindings_loader, self.identity_loader = bindings_loader, identity_loader

    def _prepare(self, actor, rule, target, facts, binding_id, observation=None):
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            raise ValueError('invalid_dispatch_target')
        facts = json.loads(encode(facts, MAX_FACT_BYTES))
        binding = self.bindings_loader().get(binding_id)
        if ('observation' in binding) != (observation is not None):
            raise PermissionError('observation_evidence_required')
        if binding['rule'] != rule:
            raise PermissionError('dispatch_rule_mismatch')
        resource = authorize(self.lifecycle, actor, binding, target)
        decision = self.lifecycle.evaluate_active(actor, rule, target, facts)
        if decision['revision'] != binding['revision'] or decision['result'] != binding['result']:
            raise Conflict('decision_does_not_match_binding')
        # Identity must still be active after evaluation, not merely at HTTP ingress.
        resource = authorize(self.lifecycle, actor, binding, target)
        credential_id = self.identity_loader(actor)
        if not isinstance(credential_id, str) or not 1 <= len(credential_id) <= 128:
            raise PermissionError('dispatch_identity_unavailable')
        return binding, resource, decision, digest(facts), credential_id

    def simulate(self, actor, rule, target, facts, binding_id):
        # Simulation never uses admission, nonce consumption, queue or actions.
        from .zen_rules import ZenRules
        binding = self.bindings_loader().get(binding_id)
        if binding['rule'] != rule or binding['organization'] != actor.organization:
            raise PermissionError('simulation_scope_denied')
        record = self.lifecycle.read(actor, rule, binding['revision'])
        self.lifecycle._authorize(self.lifecycle._policy(actor), actor, record['context'], 'test')
        if not isinstance(target, (tuple, list)) or list(target) not in record['context']['targets']:
            raise PermissionError('target_outside_rule_scope')
        facts = json.loads(encode(facts, MAX_FACT_BYTES))
        result = ZenRules(record['document']).evaluate(facts)
        self.lifecycle._authorize(self.lifecycle._policy(actor), actor, record['context'], 'test')
        structural = self.workflows.plan(binding['workflow'])
        return {'simulation': True, 'binding': binding_id, 'revision': binding['revision'],
                'target': list(target), 'result': result, 'matches_binding': result == binding['result'],
                'workflow': structural['definition'], 'input': binding['input'],
                'effects': 'none; no queue, actions or activation evidence; workflow outcomes are not simulated'}

    def plan(self, actor, rule, target, facts, binding_id, *, _observation=None):
        binding, resource, decision, _, _ = self._prepare(actor, rule, target, facts, binding_id, _observation)
        return {'binding': binding_id, 'decision': decision, 'target': resource.execution_target,
                'workflow': binding['workflow'], 'workflow_revision': digest(binding['workflow']),
                'input': binding['input'], 'max_age_seconds': binding['max_age_seconds'],
                'effects': 'none; explicit dispatch required'}

    def dispatch(self, actor, rule, target, facts, binding_id, expected_generation, event_id, *,
                 _observation=None, _conditions=None, _consume=(), _not_after=None):
        _revision(expected_generation)
        dedup = event_key(actor, rule, event_id)
        binding, resource, decision, facts_hash, credential = self._prepare(actor, rule, target, facts, binding_id, _observation)
        if decision['generation'] != expected_generation:
            raise Conflict('active_generation_changed')
        active = self.lifecycle.store.state(rule)
        if active['generation'] != expected_generation:
            raise Conflict('active_generation_changed')
        proof = {'version': 1, 'binding': binding_id, 'rule': rule, 'revision': decision['revision'],
                 'generation': expected_generation, 'result': decision['result'],
                 'facts_sha256': facts_hash, 'event_id': event_id, 'credential_id': credential}
        if _observation is not None:
            proof['observation'] = _observation
        attribution = {'subject': actor.subject, 'organization': actor.organization,
                       'issuer': ISSUER, 'workflow_revision': digest(binding['workflow'])}
        audit = {'operation': 'dispatch', 'generation': expected_generation,
                 'revision': decision['revision'], 'scope_revisions': [decision['revision']],
                 'author': actor.subject, 'organization': actor.organization, 'binding': binding_id,
                 'run_id': hashlib.sha256(dedup.encode()).hexdigest(), 'created_at': self.workflows.store.now()}
        state = self.workflows.submit(binding['workflow'], binding['input'], resource.execution_target, dedup,
            attribution=attribution, execution=proof,
            conditions=dict(_conditions or {}, **{self.lifecycle.store._key(rule, 'active'): encode(active)}),
            consume=_consume, not_after=_not_after,
            audit=(self.lifecycle.store._key(rule, 'audit'), audit))
        return self.receipt(state)

    def status(self, actor, rule, event_id):
        run_id = hashlib.sha256(event_key(actor, rule, event_id).encode()).hexdigest()
        state = self.workflows.store.get(run_id)
        attribution, proof = state.get('attribution', {}), state.get('execution', {})
        if (attribution.get('subject') != actor.subject or attribution.get('organization') != actor.organization
                or attribution.get('issuer') != ISSUER or proof.get('rule') != rule):
            raise PermissionError('dispatch_scope_denied')
        self.lifecycle.read(actor, rule, proof['revision'])
        return self.receipt(state)

    @staticmethod
    def receipt(state):
        proof = state['execution']
        result = {'id': state['id'], 'status': state['status'], 'rule': proof['rule'],
                'revision': proof['revision'], 'generation': proof['generation'],
                'binding': proof['binding'], 'event_id': proof['event_id'],
                'target': state['target'], 'history': state['history'], 'error': state.get('error')}
        if 'observation' in proof:
            result['observation'] = {'verified_at_admission': True, 'expires_at': proof['observation']['expires_at']}
        if 'reconciliation' in state:
            result['reconciliation'] = copy.deepcopy(state['reconciliation'])
        return result


class DispatchAuthorizer:
    """Reload identity, binding, policy and active generation before every attempt."""
    def __init__(self, lifecycle_factory, bindings_loader, identity_check, now, observation_check=None):
        self.lifecycle_factory, self.bindings_loader = lifecycle_factory, bindings_loader
        self.identity_check, self.now = identity_check, now
        self.observation_check = observation_check

    def __call__(self, state):
        attribution, proof = state.get('attribution', {}), state.get('execution', {})
        if (attribution.get('issuer') != ISSUER or not PROOF_FIELDS <= set(proof)
                or not set(proof) <= PROOF_FIELDS | {'observation'}
                or type(proof['version']) is not int or proof['version'] != 1):
            raise PermissionError('execution_evidence_required')
        actor = Principal(attribution['subject'], attribution['organization'])
        if self.identity_check(actor, proof['credential_id']) is not True:
            raise PermissionError('execution_identity_revoked')
        binding = self.bindings_loader().get(proof['binding'])
        if ('observation' in binding) != ('observation' in proof):
            raise PermissionError('observation_evidence_required')
        if 'observation' in binding and (self.observation_check is None
                or self.observation_check(state, binding) is not True):
            raise PermissionError('observation_evidence_invalid')
        if (binding['rule'] != proof['rule'] or binding['revision'] != proof['revision']
                or binding['result'] != proof['result'] or binding['workflow'] != state['workflow']
                or binding['input'] != state['input']
                or digest(state['workflow']) != attribution['workflow_revision']
                or not isinstance(proof['facts_sha256'], str) or not HASH.fullmatch(proof['facts_sha256'])):
            raise PermissionError('execution_evidence_mismatch')
        if state['id'] != hashlib.sha256(event_key(actor, proof['rule'], proof['event_id']).encode()).hexdigest():
            raise PermissionError('execution_identity_mismatch')
        lifecycle = self.lifecycle_factory(actor)
        # Targets originate from trusted inventory. Do not parse caller lock names.
        resources = lifecycle._policy(actor).inventory.values()
        targets = [(r.kind, r.id) for r in resources
                   if r.organization == actor.organization and r.execution_target == state['target']]
        if len(targets) != 1:
            raise PermissionError('execution_target_unavailable')
        authorize(lifecycle, actor, binding, targets[0])
        active = lifecycle.store.state(proof['rule'])
        if active['revision'] != proof['revision'] or active['generation'] != proof['generation']:
            raise PermissionError('execution_rule_changed')
        age = self.now() - state['created_at']
        if not 0 <= age <= binding['max_age_seconds']:
            raise PermissionError('execution_decision_expired')
        return True
