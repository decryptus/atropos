"""Scoped operator attestation of an uncertain outcome; never action execution."""
import copy
import hashlib
import re

from .dispatch import ISSUER, digest, event_key
from .storage_contract import Conflict

MAX_EVIDENCE_BYTES = 2048
OUTCOMES = frozenset({'succeeded', 'failed'})
HASH = re.compile(r'[0-9a-f]{64}')


def evidence_text(value):
    if (type(value) is not str or not value.strip() or value != value.strip()
            or len(value.encode()) > MAX_EVIDENCE_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('invalid_reconciliation_evidence')
    return value


def transition(state, outcome, expected_state, reconciliation):
    """Called inside the storage transaction, before any writes."""
    if (type(expected_state) is not str or not HASH.fullmatch(expected_state)
            or outcome not in OUTCOMES or type(reconciliation) is not dict):
        raise ValueError('invalid_reconciliation')
    if state['status'] != 'uncertain' or digest(state) != expected_state:
        raise Conflict('reconciliation_state_changed')
    result = copy.deepcopy(state)
    result.update(status=outcome, resolution='operator-confirmed terminal outcome',
                  reconciliation=reconciliation, updated_at=reconciliation['created_at'])
    return result


class Reconciler:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    def _load(self, actor, rule, event_id, mode):
        d = self.dispatcher
        if mode == 'real':
            store, project = d.workflows.store, d.receipt
            run_id = hashlib.sha256(event_key(actor, rule, event_id).encode()).hexdigest()
        elif mode == 'test':
            from .test_execution import test_context
            run_id = test_context(actor, rule, event_id)['id']
            store, project = d.tests.workflows.store, d.tests.receipt
        else:
            raise ValueError('invalid_reconciliation_mode')
        if store.client is not d.lifecycle.store.client:
            raise ValueError('reconciliation_requires_shared_transaction_store')
        state = store.get(run_id)
        self._authorize(actor, rule, event_id, mode, state)
        if state['status'] != 'uncertain':
            raise Conflict('run_not_reconcilable')
        return store, state, project

    def _authorize(self, actor, rule, event_id, mode, state):
        d = self.dispatcher
        owner, proof = state.get('attribution', {}), state.get('execution', {})
        if (owner.get('subject') != actor.subject or owner.get('organization') != actor.organization
                or owner.get('issuer') != (ISSUER if mode == 'real' else 'atropos.tests')
                or proof.get('rule') != rule or proof.get('event_id') != event_id
                or (mode == 'test' and state.get('test_context') != {'test': True, 'id': state['id']})
                or (mode == 'real' and 'test_context' in state)
                or owner.get('workflow_revision') != digest(state['workflow'])):
            raise PermissionError('reconciliation_scope_denied')
        record = d.lifecycle.read(actor, rule, proof['revision'])
        policy = d.lifecycle._policy(actor)
        if mode == 'test':
            d.lifecycle._authorize(policy, actor, record['context'], 'test')
        targets = [(r.kind, r.id) for r in policy.inventory.values()
                   if r.organization == actor.organization and r.execution_target == state['target']]
        actions = {step['action'] for step in state['workflow']['steps'].values()}
        if (record['context']['rule_kind'] != 'intervention' or len(targets) != 1
                or list(targets[0]) not in record['context']['targets'] or not actions
                or not policy.allows(actor.subject, actor.organization, 'intervention',
                                     'reconcile', targets, actions=actions)):
            raise PermissionError('reconciliation_scope_denied')
        credential = d.identity_loader(actor)
        if type(credential) is not str or not 1 <= len(credential) <= 128:
            raise PermissionError('reconciliation_identity_unavailable')

    def plan(self, actor, rule, event_id, mode='real'):
        _, state, project = self._load(actor, rule, event_id, mode)
        result = dict(mode=mode, expected_state=digest(state), receipt=project(state))
        self._authorize(actor, rule, event_id, mode, state)
        return result

    def confirm(self, actor, rule, event_id, *, expected_state, outcome, evidence, mode='real'):
        evidence_text(evidence)
        if (type(outcome) is not str or outcome not in OUTCOMES
                or type(expected_state) is not str or not HASH.fullmatch(expected_state)):
            raise ValueError('invalid_reconciliation')
        store, state, project = self._load(actor, rule, event_id, mode)
        if digest(state) != expected_state:
            raise Conflict('reconciliation_state_changed')
        annotation = dict(author=actor.subject, organization=actor.organization, outcome=outcome,
                          evidence=evidence, expected_state=expected_state, created_at=store.now())
        audit = dict(operation='reconcile', mode=mode, run_id=state['id'],
                     revision=state['execution']['revision'], scope_revisions=[state['execution']['revision']],
                     **annotation)
        # Admission age, activation and binding approval do not grant this right.
        # Reload identity/policy immediately before the atomic terminal transition.
        self._authorize(actor, rule, event_id, mode, state)
        result = store.reconcile(state['id'], outcome, expected_state=expected_state,
                                 reconciliation=annotation,
                                 audit=(self.dispatcher.lifecycle.store._key(rule, 'audit'), audit))
        self._authorize(actor, rule, event_id, mode, result)
        return project(result)
