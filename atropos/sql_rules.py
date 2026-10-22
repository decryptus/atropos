"""Internal immutable rule revisions; saving never activates a revision.

External providers must implement this contract with their own CAS/versioning.
This SQL implementation is storage only: an authenticated application enforces
user/domain/resource/provider permissions before calling it.
"""
import hashlib
import json
import uuid
from typing import Protocol

import time

from .definitions import encode, name
from .authorization import identifier
from .storage_contract import Conflict
from .rule_model import dump_rules, validate_rules

MAX_REVISION_BYTES = 131072


class RuleRepository(Protocol):
    def head(self, rule_name): ...
    def save(self, document, author, expected_revision, message='', context=None): ...
    def get(self, rule_name, revision): ...
    def history(self, rule_name, offset=0, limit=50): ...
    def restore(self, rule_name, revision, author, expected_revision): ...


class SQLRuleRepository:
    def __init__(self, client, organization, namespace='atropos'):
        name(namespace, 'namespace')
        self.client = client
        self.organization = identifier(organization)
        self.prefix = f'{namespace}:rules:{identifier(organization)}'

    def _key(self, rule_name, suffix):
        name(rule_name, 'rule name')
        return f'{self.prefix}:{rule_name}:{suffix}'

    def availability_key(self, rule_name):
        return self._key(rule_name, 'availability')

    def availability(self, rule_name, client=None):
        raw = (client if client is not None else self.client).get(self.availability_key(rule_name))
        return json.loads(raw) if raw else {'status': 'available', 'generation': None}

    def require_available(self, rule_name, client=None):
        if self.availability(rule_name, client)['status'] != 'available':
            raise Conflict('rule_unavailable')

    def head(self, rule_name):
        return self.client.get(self._key(rule_name, 'head'))

    def save(self, document, author, expected_revision, message='', context=None):
        document = validate_rules(document)
        identifier(author)
        if not isinstance(message, str) or len(message) > 256:
            raise ValueError('invalid_revision_message')
        head = self._key(document['name'], 'head')
        history = self._key(document['name'], 'history')
        revision = uuid.uuid4().hex
        created_at = time.time()
        record = {'revision': revision, 'parent': expected_revision, 'author': author,
                  'message': message, 'created_at': created_at,
                  'content_sha256': hashlib.sha256(encode(document).encode()).hexdigest(),
                  'document': document}
        if context is not None:
            # Generic immutable metadata. The application validates its schema.
            record['context'] = json.loads(encode(context, 32768))
            record['context_sha256'] = hashlib.sha256(encode(record['context']).encode()).hexdigest()
        with self.client.transaction():
            self.require_available(document['name'])
            if self.client.get(head) != expected_revision:
                raise Conflict('rule_head_changed')
            self.client.set(self._key(document['name'], revision), encode(record, MAX_REVISION_BYTES))
            self.client.set(head, revision)
            self.client.append(history, revision)
            return record

    def get(self, rule_name, revision):
        if not isinstance(revision, str) or len(revision) != 32 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('invalid_revision')
        raw = self.client.get(self._key(rule_name, revision))
        if raw is None:
            raise KeyError('rule_revision_not_found')
        record = json.loads(raw)
        document = validate_rules(record['document'])
        if record['content_sha256'] != hashlib.sha256(encode(document).encode()).hexdigest():
            raise ValueError('rule_revision_integrity_error')
        if record['revision'] != revision or document['name'] != rule_name:
            raise ValueError('rule_revision_identity_error')
        if 'context' in record and record.get('context_sha256') != hashlib.sha256(encode(record['context']).encode()).hexdigest():
            raise ValueError('rule_context_integrity_error')
        return record

    def history(self, rule_name, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_history_page')
        revisions = self.client.entries(self._key(rule_name, 'history'), offset, limit)
        return [{k: v for k, v in self.get(rule_name, revision).items() if k != 'document'}
                for revision in revisions]

    def restore(self, rule_name, revision, author, expected_revision):
        record = self.get(rule_name, revision)
        return self.save(record['document'], author, expected_revision,
                         message=f'Restored from {revision}', context=record.get('context'))

    def export_yaml(self, rule_name, revision):
        return dump_rules(self.get(rule_name, revision)['document'])


class SQLLifecycleStore:
    def __init__(self, repository):
        self.repository, self.client = repository, repository.client

    def _key(self, rule_name, suffix):
        return self.repository._key(rule_name, 'lifecycle:' + suffix)

    def now(self): return int(time.time())

    def save_test(self, rule_name, report):
        with self.client.transaction():
            self.repository.require_available(rule_name)
            self.client.set(self._key(rule_name,'test:'+report['id']),encode(report))
            self.client.set(self._key(rule_name,'latest-test:'+report['revision']),report['id'])
            self.client.append(self._key(rule_name,'tests:'+report['revision']),report['id'])

    def report(self, rule_name, report_id):
        raw=self.client.get(self._key(rule_name,'test:'+report_id))
        if raw is None: raise KeyError('test_report_not_found')
        return json.loads(raw)

    def latest_test(self, rule_name, revision):
        return self.client.get(self._key(rule_name,'latest-test:'+revision))

    def state(self, rule_name):
        raw=self.client.get(self._key(rule_name,'active'))
        return json.loads(raw) if raw else {'generation':None,'revision':None,'test_id':None}

    def was_active(self, rule_name, revision):
        return self.client.get(self._key(rule_name,'activated:'+revision)) is not None

    def transition(self, rule_name, *, revision, test_id, expected_generation, operation, actor):
        with self.client.transaction():
            self.repository.require_available(rule_name)
            state=self.state(rule_name)
            if state['generation'] != expected_generation: raise Conflict('active_generation_changed')
            if revision is not None and self.latest_test(rule_name,revision)!=test_id: raise Conflict('latest_test_changed')
            generation=uuid.uuid4().hex;old_revision=state.get('last_revision')
            updated=dict(generation=generation,revision=revision,test_id=test_id,last_revision=revision or old_revision)
            event=dict(generation=generation,previous_generation=state['generation'],operation=operation,
                author=actor.subject,organization=actor.organization,created_at=self.now(),
                previous_revision=state['revision'],revision=revision,test_id=test_id,
                scope_revisions=sorted({r for r in (old_revision,revision) if r}))
            self.client.set(self._key(rule_name,'active'),encode(updated))
            self.client.append(self._key(rule_name,'audit'),encode(event))
            if revision: self.client.set(self._key(rule_name,'activated:'+revision),'1')
            return updated

    def admission_history(self, rule_name, max_events):
        rows=self.client.entries(self._key(rule_name,'audit'),0,max_events+1)
        if len(rows)>max_events: raise RuntimeError('intervention_search_capacity_exceeded')
        return [json.loads(row) for row in rows]

    def audit(self, rule_name, offset, limit):
        return [json.loads(row) for row in self.client.entries(self._key(rule_name,'audit'),offset,limit)]

    def retire(self, rule_name, *, operation, actor, expected_revision, expected_generation, contexts):
        with self.client.transaction():
            r=self.repository;current=r.availability(rule_name)
            history=self.client.entries(r._key(rule_name,'history'),0,10001)
            if (r.head(rule_name)!=expected_revision or current['generation']!=expected_generation or history!=list(contexts)):
                raise Conflict('rule_availability_changed')
            if current['status']=='deleted': raise Conflict('rule_deleted')
            if self.state(rule_name)['revision'] is not None: raise Conflict('deactivate_before_retirement')
            target={'archive':'archived','unarchive':'available','delete':'deleted'}[operation]
            if operation=='archive' and current['status']!='available': raise Conflict('rule_already_archived')
            if operation=='unarchive' and current['status']!='archived': raise Conflict('rule_not_archived')
            if operation=='delete':
                if any(self.was_active(rule_name,rev) for rev in history): raise Conflict('activated_rule_requires_archive')
                if any(e['operation'] in ('dispatch','test_run') for e in self.admission_history(rule_name,10000)):
                    raise Conflict('linked_intervention_requires_archive')
                # Canonical validated names contain no glob metacharacters.
                prefix=r._key(rule_name,'')
                keys=[x[0] for x in self.client.connection.execute('SELECT "key" FROM records WHERE substr("key",1,?)=?',(len(prefix),prefix))]
                if len(keys)>10000: raise Conflict('rule_retirement_limit_exceeded')
                self.client.delete(*(k for k in keys if k!=r.availability_key(rule_name)))
                self.client.connection.execute('DELETE FROM journal WHERE substr("key",1,?)=? AND "key"<>?',(len(prefix),prefix,self._key(rule_name,'audit')))
            generation=uuid.uuid4().hex
            updated=dict(status=target,generation=generation,revision=expected_revision)
            if operation=='delete': updated['contexts']=contexts
            event=dict(operation=operation,generation=generation,previous_generation=current['generation'],
                author=actor.subject,organization=actor.organization,created_at=self.now(),revision=expected_revision,scope_revisions=list(contexts))
            self.client.set(r.availability_key(rule_name),encode(updated))
            self.client.append(self._key(rule_name,'audit'),encode(event))
            return {k:v for k,v in updated.items() if k!='contexts'}
