"""Redis persistence for test evidence, active generations and lifecycle audit."""
import json
import uuid

from redis.exceptions import WatchError

from .definitions import encode
from .storage_contract import Conflict


class RedisLifecycleStore:
    def __init__(self, repository):
        self.repository = repository
        self.client = repository.client

    def _key(self, rule_name, suffix):
        return self.repository._key(rule_name, 'lifecycle:' + suffix)

    def now(self):
        return self.client.time()[0]

    def save_test(self, rule_name, report):
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(self.repository.availability_key(rule_name))
                self.repository.require_available(rule_name, pipe)
                pipe.multi()
                pipe.set(self._key(rule_name, 'test:' + report['id']), encode(report), nx=True)
                pipe.set(self._key(rule_name, 'latest-test:' + report['revision']), report['id'])
                pipe.rpush(self._key(rule_name, 'tests:' + report['revision']), report['id'])
                pipe.incr(self._key(rule_name, 'test-epoch'))
                pipe.execute()
            except WatchError as exc:
                raise Conflict('rule_availability_changed') from exc

    def report(self, rule_name, report_id):
        raw = self.client.get(self._key(rule_name, 'test:' + report_id))
        if raw is None:
            raise KeyError('test_report_not_found')
        return json.loads(raw)

    def latest_test(self, rule_name, revision):
        return self.client.get(self._key(rule_name, 'latest-test:' + revision))

    def state(self, rule_name, client=None):
        raw = (client if client is not None else self.client).get(self._key(rule_name, 'active'))
        return json.loads(raw) if raw else {'generation': None, 'revision': None, 'test_id': None}

    def was_active(self, rule_name, revision):
        return bool(self.client.sismember(self._key(rule_name, 'activated-revisions'), revision))

    def transition(self, rule_name, *, revision, test_id, expected_generation, operation, actor):
        active_key = self._key(rule_name, 'active')
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(active_key, self.repository.availability_key(rule_name))
                self.repository.require_available(rule_name, pipe)
                state = self.state(rule_name, pipe)
                if state['generation'] != expected_generation:
                    raise Conflict('active_generation_changed')
                if revision is not None:
                    test_key = self._key(rule_name, 'latest-test:' + revision)
                    pipe.watch(test_key)
                    if pipe.get(test_key) != test_id:
                        raise Conflict('latest_test_changed')
                generation = uuid.uuid4().hex
                old_revision = state.get('last_revision')
                updated = {'generation': generation, 'revision': revision, 'test_id': test_id,
                           'last_revision': revision or old_revision}
                event = {'generation': generation, 'previous_generation': state['generation'],
                         'operation': operation, 'author': actor.subject,
                         'organization': actor.organization, 'created_at': self.now(),
                         'previous_revision': state['revision'], 'revision': revision, 'test_id': test_id,
                         'scope_revisions': sorted({r for r in (old_revision, revision) if r})}
                pipe.multi()
                pipe.set(active_key, encode(updated))
                pipe.rpush(self._key(rule_name, 'audit'), encode(event))
                if revision:
                    pipe.sadd(self._key(rule_name, 'activated-revisions'), revision)
                pipe.execute()
                return updated
            except WatchError as exc:
                raise Conflict('lifecycle_state_changed') from exc

    def iter_admissions(self, rule_name, page_size):
        if type(page_size) is not int or page_size<1:
            raise RuntimeError('invalid_history_page_size')
        key=self._key(rule_name,'audit')
        size=self.client.llen(key)
        for offset in range(0,size,page_size):
            rows=self.client.lrange(key,offset,min(size,offset+page_size)-1)
            if len(rows)!=min(page_size,size-offset):
                raise Conflict('intervention_history_changed')
            for row in rows: yield json.loads(row)
        if self.client.llen(key)!=size: raise Conflict('intervention_history_changed')

    def admission_history(self, rule_name, max_events):
        # One bounded Redis read includes all retained events or fails. An empty
        # filtered window must never be mistaken for end-of-history.
        rows=self.client.lrange(self._key(rule_name,'audit'),0,max_events)
        if len(rows)>max_events:raise RuntimeError('intervention_search_capacity_exceeded')
        return [json.loads(raw) for raw in rows]

    def audit(self, rule_name, offset, limit):
        return [json.loads(raw) for raw in self.client.lrange(
            self._key(rule_name, 'audit'), offset, offset + limit - 1)]

    def retire(self, rule_name, *, operation, actor, expected_revision,
               expected_generation, contexts):
        """Atomic archive/unarchive/delete after authorization of all revision scopes."""
        repository = self.repository
        head = repository._key(rule_name, 'head')
        history = repository._key(rule_name, 'history')
        availability = repository.availability_key(rule_name)
        active = self._key(rule_name, 'active')
        audit = self._key(rule_name, 'audit')
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(head, history, availability, active, audit,
                           self._key(rule_name, 'activated-revisions'), self._key(rule_name, 'test-epoch'))
                current = repository.availability(rule_name, pipe)
                if (pipe.get(head) != expected_revision or current['generation'] != expected_generation
                        or pipe.lrange(history, 0, -1) != list(contexts)):
                    raise Conflict('rule_availability_changed')
                if current['status'] == 'deleted': raise Conflict('rule_deleted')
                if self.state(rule_name, pipe)['revision'] is not None:
                    raise Conflict('deactivate_before_retirement')
                target = {'archive': 'archived', 'unarchive': 'available', 'delete': 'deleted'}[operation]
                if operation == 'archive' and current['status'] != 'available':
                    raise Conflict('rule_already_archived')
                if operation == 'unarchive' and current['status'] != 'archived':
                    raise Conflict('rule_not_archived')
                removable = []
                if operation == 'delete':
                    if pipe.scard(self._key(rule_name, 'activated-revisions')):
                        raise Conflict('activated_rule_requires_archive')
                    # Any linked real or simulated intervention permanently excludes
                    # draft deletion. Watching audit fences concurrent admission.
                    if any(json.loads(row)['operation'] in ('dispatch', 'test_run')
                           for row in pipe.lrange(audit, 0, -1)):
                        raise Conflict('linked_intervention_requires_archive')
                    removable = list(self.client.scan_iter(match=repository._key(rule_name, '*'), count=100))
                    if len(removable) > 10000: raise Conflict('rule_retirement_limit_exceeded')
                    removable = [key for key in removable if key not in (audit, availability)]
                generation = uuid.uuid4().hex
                updated = {'status': target, 'generation': generation, 'revision': expected_revision}
                if operation == 'delete': updated['contexts'] = contexts
                event = {'operation': operation, 'generation': generation,
                         'previous_generation': current['generation'], 'author': actor.subject,
                         'organization': actor.organization, 'created_at': self.now(),
                         'revision': expected_revision, 'scope_revisions': list(contexts)}
                pipe.multi()
                if removable: pipe.delete(*removable)
                pipe.set(availability, encode(updated))
                pipe.rpush(audit, encode(event))
                pipe.execute()
                return {key: value for key, value in updated.items() if key != 'contexts'}
            except WatchError as exc:
                raise Conflict('rule_availability_changed') from exc
