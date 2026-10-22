"""Engine-issued challenges and source-bound observations signed by a trusted gateway."""
import hashlib
import json
import math
import re
import uuid

from atropos_client.observation_signing import verify_observation
from .authorization import identifier
from .definitions import encode
from .dispatch import authorize, digest
from .storage_contract import Conflict

CHALLENGE_TTL = 60
SPEC_FIELDS = frozenset({'key_id','source_id','native_id','filesystem','max_age_seconds'})
PAYLOAD_FIELDS = frozenset({'challenge','facts','source','age_seconds','max_age_seconds'})
SOURCE_FIELDS = frozenset({'source_id','native_id','filesystem'})


def validate_spec(value):
    if type(value) is not dict or set(value)!=SPEC_FIELDS:
        raise ValueError('invalid_observation_requirement')
    identifier(value['key_id']);identifier(value['source_id'])
    if not isinstance(value['native_id'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['native_id']):
        raise ValueError('invalid_observation_requirement')
    fs=value['filesystem']
    if fs is not None and (not isinstance(fs,list) or len(fs)!=2 or
            any(not isinstance(v,str) or not 0<len(v)<=4096 or not v.isprintable() for v in fs)):
        raise ValueError('invalid_observation_requirement')
    if type(value['max_age_seconds']) is not int or not 1<=value['max_age_seconds']<=300:
        raise ValueError('invalid_observation_requirement')


def checked_payload(token, binding, keys, now):
    key_id,payload=verify_observation(token,keys)
    specification=binding.get('observation')
    if specification is None:raise PermissionError('observation_binding_required')
    if (type(payload) is not dict or set(payload)!=PAYLOAD_FIELDS
            or key_id!=specification['key_id'] or type(payload['source']) is not dict
            or set(payload['source'])!=SOURCE_FIELDS
            or payload['source']!={k:specification[k] for k in SOURCE_FIELDS}
            or type(payload['facts']) is not dict):
        raise PermissionError('observation_source_mismatch')
    challenge=payload['challenge']
    if type(challenge) is not dict:raise ValueError('invalid_observation_challenge')
    age,maximum=payload['age_seconds'],payload['max_age_seconds']
    values=(age,maximum,challenge.get('issued_at'),challenge.get('expires_at'),now)
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
        raise ValueError('invalid_observation_age')
    issued=challenge['issued_at']
    if (age<0 or maximum<=0 or challenge['expires_at']!=issued+CHALLENGE_TTL
            or now<issued or set(payload['facts'])!=set(challenge['inputs'])):
        raise PermissionError('invalid_observation_evidence')
    expires=min(challenge['expires_at'],issued+min(maximum,specification['max_age_seconds'])-age)
    if now>=expires:raise PermissionError('observation_expired')
    return payload,expires


def verify_run_observation(state, binding, keys, now):
    evidence=state['execution'].get('observation')
    if type(evidence) is not dict or set(evidence)!={'token','expires_at'}:
        raise PermissionError('observation_evidence_required')
    payload,expires=checked_payload(evidence['token'],binding,keys,now)
    challenge=payload['challenge'];proof=state['execution'];attribution=state['attribution']
    if (evidence['expires_at']!=expires or not challenge['issued_at']<=state['created_at']<expires
            or challenge['subject']!=attribution['subject'] or challenge['organization']!=attribution['organization']
            or challenge['rule']!=proof['rule'] or challenge['revision']!=proof['revision']
            or challenge['generation']!=proof['generation'] or challenge['binding']!=proof['binding']
            or digest(payload['facts'])!=proof['facts_sha256']
            or state['target']!='/'.join([attribution['organization'],*challenge['target']])):
        raise PermissionError('observation_evidence_mismatch')
    return True


class ObservationAuthority:
    def __init__(self, dispatcher, keys_loader):
        self.dispatcher=dispatcher
        self.keys_loader=keys_loader
        self.store=dispatcher.workflows.store

    def slot(self, actor, credential):
        # One outstanding challenge per authenticated engine credential. Bounded
        # temporary storage; a concurrent new challenge invalidates the previous one.
        value=encode([actor.subject,actor.organization,credential])
        return self.store.prefix+':observation-challenge:'+hashlib.sha256(value.encode()).hexdigest()

    def challenge(self, actor, rule, target, binding_id):
        binding=self.dispatcher.bindings_loader().get(binding_id)
        if binding['rule']!=rule or 'observation' not in binding:
            raise PermissionError('observation_binding_required')
        if binding['observation']['key_id'] not in self.keys_loader():
            raise PermissionError('observation_key_unavailable')
        authorize(self.dispatcher.lifecycle,actor,binding,target)
        active=self.dispatcher.lifecycle.active(actor,rule)
        if active['revision']!=binding['revision']:raise Conflict('active_revision_changed')
        record=self.dispatcher.lifecycle.read(actor,rule,active['revision'])
        credential=self.dispatcher.identity_loader(actor)
        now=self.store.now()
        challenge={'id':uuid.uuid4().hex,'subject':actor.subject,'organization':actor.organization,
                   'rule':rule,'revision':active['revision'],'generation':active['generation'],
                   'target':list(target),'binding':binding_id,'inputs':record['document']['inputs'],
                   'issued_at':now,'expires_at':now+CHALLENGE_TTL}
        self.store.client.set(self.slot(actor,credential),encode(challenge),ex=CHALLENGE_TTL)
        return challenge

    def verify(self, actor, token):
        credential=self.dispatcher.identity_loader(actor)
        key=self.slot(actor,credential);raw=self.store.client.get(key)
        if raw is None:raise PermissionError('observation_challenge_unavailable')
        challenge=json.loads(raw)
        binding=self.dispatcher.bindings_loader().get(challenge['binding'])
        payload,expires=checked_payload(token,binding,self.keys_loader(),self.store.now())
        if payload['challenge']!=challenge:
            raise PermissionError('observation_challenge_mismatch')
        return challenge,payload,{'token':token,'expires_at':expires},key,raw

    def plan(self, actor, token):
        c,p,evidence,_,_=self.verify(actor,token)
        result=self.dispatcher.plan(actor,c['rule'],c['target'],p['facts'],c['binding'],_observation=evidence)
        if result['decision']['generation'] != c['generation'] or result['decision']['revision'] != c['revision']:
            raise Conflict('active_generation_changed')
        # Preview only: discard if evaluation consumed the remaining age budget.
        checked_payload(token,self.dispatcher.bindings_loader().get(c['binding']),self.keys_loader(),self.store.now())
        return dict(result,facts=p['facts'],observation=dict(p['source'],expires_at=evidence['expires_at']))

    def dispatch(self, actor, token, expected_generation, event_id):
        c,p,evidence,key,raw=self.verify(actor,token)
        if c['generation']!=expected_generation:raise Conflict('active_generation_changed')
        return self.dispatcher.dispatch(actor,c['rule'],c['target'],p['facts'],c['binding'],expected_generation,event_id,
                     _observation=evidence,_conditions={key:raw},_consume=(key,),_not_after=evidence['expires_at'])
