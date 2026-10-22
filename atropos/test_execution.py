"""Isolated end-to-end test runs. Explicit simulators only; no real action fallback."""
import hashlib
import json

from .definitions import encode, name
from .authorization import identifier
from .dispatch import MAX_FACT_BYTES, digest, event_key
from .rule_lifecycle import Principal
from .storage_contract import Conflict

TEST_PROOF_FIELDS = frozenset({'version','binding','rule','revision','event_id','credential_id','test_event'})


def test_namespace(namespace):
    return 'tests-'+hashlib.sha256(namespace.encode()).hexdigest()[:48]


def test_key(actor, rule, event_id):
    return 'test:'+event_key(actor,rule,event_id)


def test_context(actor, rule, event_id):
    return {'test':True,'id':hashlib.sha256(test_key(actor,rule,event_id).encode()).hexdigest()}


def authorize_test(lifecycle, actor, binding, target):
    lifecycle.repository.require_available(binding['rule'])
    record=lifecycle.read(actor,binding['rule'],binding['revision'])
    policy=lifecycle._policy(actor)
    lifecycle._authorize(policy,actor,record['context'],'test')
    actions={step['action'] for step in binding['workflow']['steps'].values()}
    if (binding['organization']!=actor.organization or list(target) not in record['context']['targets']
            or not policy.allows(actor.subject,actor.organization,record['context']['rule_kind'],'test',[target],actions=actions)):
        raise PermissionError('test_action_scope_denied')
    return policy.resource(actor.organization,*target)


class RuleTestRunner:
    def __init__(self, dispatcher, workflows):
        self.dispatcher,self.workflows=dispatcher,workflows

    def bindings(self, actor, rule, target):
        """Bounded metadata only; admission always rechecks current authorization."""
        name(rule, 'rule')
        if (not isinstance(target, (list, tuple)) or len(target) != 2
                or target[0] not in ('server', 'container')):
            raise ValueError('invalid_test_target')
        identifier(target[1])
        d = self.dispatcher
        records = d.bindings_loader().records
        result = []
        for binding_id, binding in sorted(records.items()):
            if binding['organization'] != actor.organization or binding['rule'] != rule:
                continue
            try:
                authorize_test(d.lifecycle, actor, binding, target)
            except (PermissionError, KeyError, Conflict):
                # No existence or workflow metadata for inaccessible revisions.
                continue
            actions = sorted({step['action'] for step in binding['workflow']['steps'].values()})
            result.append({'id': binding_id, 'rule': rule, 'revision': binding['revision'],
                           'workflow': binding['workflow']['name'], 'actions': actions,
                           'test_supported': all(self.workflows.actions[action].simulate is not None
                                                 for action in actions)})
        return result

    def submit(self, actor, rule, target, facts, binding_id, event_id):
        d=self.dispatcher
        facts=json.loads(encode(facts,MAX_FACT_BYTES))
        binding=d.bindings_loader().get(binding_id)
        disposition_key=d.lifecycle.repository.availability_key(rule)
        disposition_raw=d.lifecycle.repository.client.get(disposition_key)
        resource=authorize_test(d.lifecycle,actor,binding,target)
        simulation=d.simulate(actor,rule,target,facts,binding_id)
        context=test_context(actor,rule,event_id)
        if not simulation['matches_binding']:
            return dict(simulation,test_context=context,status='not_matched')
        # Inputs stay fixed by the reviewed binding. The source event and decision
        # carry the same immutable test marker; they cannot be submitted as real work.
        event={'test_context':context,'facts':facts,'decision':{'test_context':context,'result':simulation['result']}}
        proof={'version':1,'binding':binding_id,'rule':rule,'revision':binding['revision'],
               'event_id':event_id,'credential_id':d.identity_loader(actor),'test_event':event}
        state=self.workflows.submit(binding['workflow'],binding['input'],resource.execution_target,
              test_key(actor,rule,event_id),attribution={'subject':actor.subject,'organization':actor.organization,
                  'issuer':'atropos.tests','workflow_revision':digest(binding['workflow'])},
              execution=proof,test_context=context,
              conditions={disposition_key:disposition_raw},
              audit=(d.lifecycle.store._key(rule,'audit'),{'operation':'test_run','author':actor.subject,
                  'organization':actor.organization,'scope_revisions':[binding['revision']],
                  'revision':binding['revision'],'test_context':context,'run_id':context['id'],
                  'created_at':self.workflows.store.now()}))
        return self.receipt(state)

    def status(self, actor, rule, event_id):
        context=test_context(actor,rule,event_id)
        state=self.workflows.store.get(context['id'])
        a,p=state.get('attribution',{}),state.get('execution',{})
        if (a.get('subject')!=actor.subject or a.get('organization')!=actor.organization
                or a.get('issuer')!='atropos.tests' or p.get('rule')!=rule):
            raise PermissionError('test_scope_denied')
        record=self.dispatcher.lifecycle.read(actor,rule,p['revision'])
        self.dispatcher.lifecycle._authorize(self.dispatcher.lifecycle._policy(actor),actor,record['context'],'test')
        return self.receipt(state)

    @staticmethod
    def receipt(state):
        result = {'id':state['id'],'status':state['status'],'test_context':state['test_context'],
                'rule':state['execution']['rule'],'revision':state['execution']['revision'],
                'history':state['history'],'results':state['results'],'error':state.get('error')}
        if 'reconciliation' in state:
            import copy
            result['reconciliation'] = copy.deepcopy(state['reconciliation'])
        return result


class TestRunAuthorizer:
    def __init__(self, lifecycle_factory, bindings_loader, identity_check, now):
        self.lifecycle_factory,self.bindings_loader=lifecycle_factory,bindings_loader
        self.identity_check,self.now=identity_check,now

    def __call__(self, state):
        a,p=state.get('attribution',{}),state.get('execution',{})
        if a.get('issuer')!='atropos.tests' or set(p)!=TEST_PROOF_FIELDS or type(p['version']) is not int or p['version']!=1:
            raise PermissionError('test_evidence_required')
        actor=Principal(a['subject'],a['organization'])
        context=test_context(actor,p['rule'],p['event_id'])
        if (state.get('test_context')!=context or state['id']!=context['id']
                or p['test_event']['test_context']!=context or p['test_event']['decision']['test_context']!=context
                or self.identity_check(actor,p['credential_id']) is not True):
            raise PermissionError('test_identity_mismatch')
        binding=self.bindings_loader().get(p['binding'])
        if (binding['revision']!=p['revision'] or binding['rule']!=p['rule']
                or binding['workflow']!=state['workflow'] or binding['input']!=state['input']
                or digest(state['workflow'])!=a['workflow_revision']
                or p['test_event']['decision']['result']!=binding['result']):
            raise PermissionError('test_evidence_mismatch')
        lifecycle=self.lifecycle_factory(actor)
        targets=[(r.kind,r.id) for r in lifecycle._policy(actor).inventory.values()
                 if r.organization==actor.organization and r.execution_target==state['target']]
        if len(targets)!=1:raise PermissionError('test_target_missing')
        authorize_test(lifecycle,actor,binding,targets[0])
        if not 0<=self.now()-state['created_at']<=binding['max_age_seconds']:
            raise PermissionError('test_run_expired')
        return True
