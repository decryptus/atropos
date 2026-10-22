"""Shared validation and immutable identity for workflow admission."""
import hashlib
import json
import math
import uuid
from .definitions import encode

def admission_state(now, workflow, inputs, target, deduplication_key=None, attribution=None, *, execution=None, conditions=None, audit=None, consume=(), not_after=None, test_context=None):
    if not isinstance(inputs, dict) or not isinstance(target, str) or not 1 <= len(target) <= 256:
        raise ValueError('input must be an object and target a nonempty string <= 256 characters')
    if deduplication_key is not None and (not isinstance(deduplication_key, str) or not 1 <= len(deduplication_key) <= 256):
        raise ValueError('deduplication key must contain 1–256 characters')
    if attribution is not None and (not isinstance(attribution, dict)
            or set(attribution) != {'subject', 'organization', 'issuer', 'workflow_revision'}
            or any(not isinstance(value, str) or not 1 <= len(value) <= 128
                   for value in attribution.values())):
        raise ValueError('invalid_attribution')
    # Trusted admission conditions are checked atomically with queue insertion.
    conditions = conditions or {}
    if not isinstance(conditions, dict) or len(conditions) > 8 or any(
            not isinstance(k, str) or (v is not None and not isinstance(v, str)) for k, v in conditions.items()):
        raise ValueError('invalid_admission_conditions')
    if not isinstance(consume, (list, tuple)) or any(k not in conditions for k in consume):
        raise ValueError('invalid_admission_consumption')
    if not_after is not None and (type(not_after) not in (int, float) or not math.isfinite(not_after)):
        raise ValueError('invalid_admission_deadline')
    if execution is not None and (not isinstance(execution, dict) or attribution is None):
        raise ValueError('invalid_execution_evidence')
    if audit is not None and (not isinstance(audit, tuple) or len(audit) != 2
                              or not isinstance(audit[0], str) or not isinstance(audit[1], dict)):
        raise ValueError('invalid_admission_audit')
    if test_context is not None and (type(test_context) is not dict or set(test_context) != {'test','id'}
            or test_context['test'] is not True or not isinstance(test_context['id'], str)
            or len(test_context['id']) != 64 or attribution is None):
        raise ValueError('invalid_test_context')
    identity_data = {'workflow': workflow, 'input': inputs, 'target': target}
    if test_context is not None:
        identity_data['test_context'] = dict(test_context)
    if execution is not None:
        identity_data['execution'] = execution
    if attribution is not None:
        identity_data['attribution'] = attribution
    identity = encode(identity_data)
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    run_id = hashlib.sha256(uuid.uuid4().bytes).hexdigest() if deduplication_key is None else hashlib.sha256(deduplication_key.encode()).hexdigest()
    state = {'id': run_id, 'fingerprint': fingerprint, 'workflow': workflow, 'input': inputs,
             'target': target, 'current': workflow['start'], 'status': 'queued', 'attempts': {},
             'results': {}, 'history': [], 'created_at': now()}
    if test_context is not None:
        state['test_context'] = dict(test_context)
    if attribution is not None:
        state['attribution'] = dict(attribution)
    if execution is not None:
        state['execution'] = json.loads(encode(execution))
    return state
