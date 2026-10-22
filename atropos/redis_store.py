"""Redis Streams backend. All durable transitions use optimistic transactions."""
import hashlib
import json
import uuid
from .storage_contract import Claim, Conflict, QueueFull

from redis.exceptions import ResponseError, WatchError

from .definitions import encode, name
from .maintenance import PAUSE_SUFFIX

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
GROUP = 'workers-v1'
MAX_STATE_BYTES = 1048576
DEFAULT_QUEUE_LIMIT = 1000
MAX_QUEUE_LIMIT = 1000000
PROMOTE = """
local items = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 100)
for _, id in ipairs(items) do
  redis.call('XADD', KEYS[2], '*', 'run_id', id)
  redis.call('ZREM', KEYS[1], id)
end
return #items
"""


def queue_limit(value):
    if type(value) is not int or not 0 <= value <= MAX_QUEUE_LIMIT:
        raise ValueError('invalid_queue_limit')
    return value


def stored_queue_limit(raw):
    if raw is None:
        return DEFAULT_QUEUE_LIMIT
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 7:
        raise ValueError('invalid_queue_limit')
    return queue_limit(int(raw))


class RedisStore:
    def __init__(self, client, namespace='atropos', lease_ms=30000):
        name(namespace, 'namespace')
        if type(lease_ms) is not int or lease_ms < 300:
            raise ValueError('lease_ms must be >= 300')
        self.client, self.lease_ms = client, lease_ms
        self.prefix = namespace
        self.stream = f'{namespace}:queue'
        self.delayed = f'{namespace}:delayed'
        self.quarantine = f'{namespace}:uncertain'
        self.dead = f'{namespace}:failed'
        self.capacity = f'{namespace}:queue-limit'
        self.execution_pause = namespace + PAUSE_SUFFIX

    def configure_queue(self, max_outstanding):
        # Trusted host administration. Every producer reads this shared value
        # within its admission transaction; no process-local policy override.
        self.client.set(self.capacity, str(queue_limit(max_outstanding)))
        return self.queue_status()

    def queue_status(self):
        with self.client.pipeline(transaction=True) as pipe:
            pipe.get(self.capacity)
            pipe.xlen(self.stream)
            pipe.zcard(self.delayed)
            pipe.scard(self.quarantine)
            raw, pending, delayed, uncertain = pipe.execute()
        limit = stored_queue_limit(raw)
        total = pending + delayed + uncertain
        return {'namespace': self.prefix, 'max_outstanding': limit,
                'pending_entries': pending, 'delayed_entries': delayed,
                'uncertain_runs': uncertain, 'outstanding': total,
                'available': max(0, limit - total), 'full': total >= limit}

    def initialize(self):
        try:
            self.client.xgroup_create(self.stream, GROUP, id='0', mkstream=True)
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def key(self, run_id):
        return f'{self.prefix}:run:{run_id}'

    def lease(self, run_id):
        return f'{self.prefix}:lease:{run_id}'

    def target_key(self, target):
        return f'{self.prefix}:target:{hashlib.sha256(target.encode()).hexdigest()}'

    def now(self):
        seconds, microseconds = self.client.time()
        return seconds + microseconds / 1000000

    def get(self, run_id):
        raw = self.client.get(self.key(run_id))
        if raw is None:
            raise KeyError(run_id)
        return json.loads(raw)

    def enqueue(self, workflow, inputs, target, deduplication_key=None, attribution=None, *, execution=None, conditions=None, audit=None, consume=(), not_after=None, test_context=None):
        from .admission import admission_state
        state = admission_state(self.now, workflow, inputs, target, deduplication_key, attribution,
            execution=execution, conditions=conditions, audit=audit, consume=consume,
            not_after=not_after, test_context=test_context)
        run_id, fingerprint = state['id'], state['fingerprint']
        key = self.key(run_id)
        conditions = conditions or {}
        while True:
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key, self.capacity, self.stream, self.delayed, self.quarantine, *conditions)
                    if any(pipe.get(k) != value for k, value in conditions.items()):
                        raise Conflict('admission_precondition_changed')
                    if not_after is not None:
                        seconds, micros = pipe.time()
                        if seconds + micros / 1000000 >= not_after:
                            raise Conflict('admission_deadline_expired')
                    old = pipe.get(key)
                    if old:
                        if json.loads(old)['fingerprint'] != fingerprint:
                            raise Conflict('deduplication key already used with different content')
                        return json.loads(old)
                    limit = stored_queue_limit(pipe.get(self.capacity))
                    outstanding = pipe.xlen(self.stream) + pipe.zcard(self.delayed) + pipe.scard(self.quarantine)
                    if outstanding >= limit:
                        raise QueueFull('rule_queue_full')
                    pipe.multi()
                    pipe.set(key, encode(state, MAX_STATE_BYTES))
                    pipe.xadd(self.stream, {'run_id': run_id})
                    if consume:
                        pipe.delete(*consume)
                    if audit is not None:
                        pipe.rpush(audit[0], encode(audit[1]))
                    pipe.execute()
                    return state
                except WatchError:
                    continue

    def promote(self):
        return self.client.eval(PROMOTE, 2, self.delayed, self.stream, self.now())

    def receive(self, consumer, cursor='0-0', block_ms=1000):
        if self.client.exists(self.execution_pause):
            # Recover expired in-flight leases without starting queued work.
            claimed = self.client.xautoclaim(self.stream, GROUP, consumer, self.lease_ms,
                                            start_id=cursor, count=1)
            messages = claimed[1]
            if messages and self.get(messages[0][1]['run_id'])['status'] == 'running':
                return messages[0], claimed[0]
            return None, claimed[0]
        self.promote()
        claimed = self.client.xautoclaim(self.stream, GROUP, consumer, self.lease_ms,
                                        start_id=cursor, count=1)
        cursor, messages = claimed[0], claimed[1]
        if not messages:
            options = {'count': 1}
            if block_ms:
                options['block'] = block_ms
            batches = self.client.xreadgroup(GROUP, consumer, {self.stream: '>'}, **options)
            messages = batches[0][1] if batches else []
        return (messages[0] if messages else None), cursor

    def _ack(self, pipe, message_id):
        pipe.xack(self.stream, GROUP, message_id)
        pipe.xdel(self.stream, message_id)

    def claim(self, message_id, run_id, consumer):
        key, lease = self.key(run_id), self.lease(run_id)
        token = uuid.uuid4().hex
        while True:
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key, lease, self.execution_pause, self.stream)
                    delivery = pipe.xrange(self.stream, message_id, message_id, count=1)
                    if not delivery or delivery[0][1].get('run_id') != run_id:
                        return None
                    raw = pipe.get(key)
                    if raw is None:
                        raise Conflict('queue references a missing run; repair storage before continuing')
                    state = json.loads(raw)
                    if pipe.exists(self.execution_pause) and state['status'] != 'running':
                        return None
                    if pipe.get(lease):
                        return None
                    target = self.target_key(state['target'])
                    pipe.watch(target)
                    owner = pipe.get(target)
                    if state['status'] in TERMINAL or state['status'] == 'uncertain':
                        pipe.multi()
                        self._ack(pipe, message_id)
                        pipe.execute()
                        return None
                    if state['status'] == 'running':
                        # An expired lease never proves the remote action stopped.
                        state['status'] = 'uncertain'
                        state['error'] = 'worker lease expired; action outcome requires reconciliation'
                        state['history'].append(dict(step=state['current'], status='uncertain', at=self.now(),
                            **({'test_context': state['test_context']} if 'test_context' in state else {})))
                        pipe.multi()
                        pipe.set(key, encode(state, MAX_STATE_BYTES))
                        pipe.sadd(self.quarantine, run_id)
                        if owner is None:
                            pipe.set(target, run_id)
                        self._ack(pipe, message_id)
                        pipe.execute()
                        return None
                    available_at = state.get('available_at', 0)
                    if available_at > self.now():
                        pipe.multi()
                        pipe.zadd(self.delayed, {run_id: available_at})
                        self._ack(pipe, message_id)
                        pipe.execute()
                        return None
                    if owner is not None and owner != run_id:
                        pipe.multi()
                        pipe.zadd(self.delayed, {run_id: self.now() + .5})
                        self._ack(pipe, message_id)
                        pipe.execute()
                        return None
                    state['status'] = 'running'
                    step = state['current']
                    state['attempts'][step] = state['attempts'].get(step, 0) + 1
                    state['history'].append(dict(step=step, status='running', at=self.now(),
                        **({'test_context': state['test_context']} if 'test_context' in state else {})))
                    pipe.multi()
                    pipe.set(target, run_id)  # Held throughout the workflow, including uncertainty.
                    pipe.set(lease, token, px=self.lease_ms)
                    pipe.set(key, encode(state, MAX_STATE_BYTES))
                    pipe.execute()
                    return Claim(state, token, message_id, consumer)
                except WatchError:
                    continue

    def heartbeat(self, claim):
        lease = self.lease(claim.state['id'])
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(lease)
                if pipe.get(lease) != claim.token:
                    return False
                pipe.multi()
                pipe.pexpire(lease, self.lease_ms)
                pipe.xclaim(self.stream, GROUP, claim.consumer, 0, [claim.message_id], justid=True)
                pipe.execute()
                return True
            except WatchError:
                return False

    def finish(self, claim, state, delay=0):
        key, lease = self.key(state['id']), self.lease(state['id'])
        target = self.target_key(state['target'])
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(lease, key, target)
                if pipe.get(lease) != claim.token or pipe.get(target) != state['id']:
                    return False  # Fencing: stale workers cannot commit results.
                state['updated_at'] = self.now()
                state['available_at'] = state['updated_at'] + delay
                pipe.multi()
                pipe.set(key, encode(state, MAX_STATE_BYTES))
                pipe.delete(lease)
                self._ack(pipe, claim.message_id)
                if state['status'] in TERMINAL:
                    pipe.delete(target)
                    if state['status'] == 'failed':
                        pipe.sadd(self.dead, state['id'])
                elif state['status'] == 'uncertain':
                    pipe.sadd(self.quarantine, state['id'])
                elif delay:
                    pipe.zadd(self.delayed, {state['id']: state['updated_at'] + delay})
                else:
                    pipe.xadd(self.stream, {'run_id': state['id']})
                pipe.execute()
                return True
            except WatchError:
                return False

    def reconcile(self, run_id, outcome, *, expected_state, reconciliation, audit):
        from .reconciliation import transition
        key, lease = self.key(run_id), self.lease(run_id)
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(key, lease, audit[0], self.quarantine, self.dead)
                raw = pipe.get(key)
                if raw is None:
                    raise KeyError(run_id)
                state = json.loads(raw)
                target = self.target_key(state['target'])
                pipe.watch(target)
                if pipe.get(lease):
                    raise Conflict('run_has_active_lease')
                if pipe.get(target) != run_id:
                    raise Conflict('target_ownership_changed')
                # MULTI does not roll back runtime WRONGTYPE errors. Watch and
                # validate every collection before queuing any mutations.
                for collection, kind in ((audit[0], 'list'), (self.quarantine, 'set'), (self.dead, 'set')):
                    if pipe.type(collection) not in ('none', kind):
                        raise Conflict('reconciliation_storage_invalid')
                result = transition(state, outcome, expected_state, reconciliation)
                encoded, event = encode(result, MAX_STATE_BYTES), encode(audit[1])
                pipe.multi()
                pipe.set(key, encoded)
                pipe.rpush(audit[0], event)
                pipe.delete(target)
                pipe.srem(self.quarantine, run_id)
                if outcome == 'failed':
                    pipe.sadd(self.dead, run_id)
                pipe.execute()
                return result
            except WatchError:
                raise Conflict('reconciliation_state_changed') from None

    def resolve(self, run_id, outcome):
        """Record an operator-confirmed result. Never resubmit an uncertain action."""
        if outcome not in ('succeeded', 'failed'):
            raise ValueError('resolution must be succeeded or failed')
        key, lease = self.key(run_id), self.lease(run_id)
        while True:
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key, lease)
                    state = self.get(run_id)
                    if state['status'] != 'uncertain' or pipe.get(lease):
                        raise Conflict('only an uncertain run without an active lease can be reconciled')
                    # Resolution terminates the run: results are not fabricated for later steps.
                    state['status'] = outcome
                    state['resolution'] = 'operator-confirmed terminal outcome'
                    state['updated_at'] = self.now()
                    target = self.target_key(state['target'])
                    pipe.watch(target)
                    if pipe.get(target) != run_id:
                        raise Conflict('target ownership changed; manual investigation required')
                    pipe.multi()
                    pipe.set(key, encode(state, MAX_STATE_BYTES))
                    pipe.delete(target)
                    pipe.srem(self.quarantine, run_id)
                    if outcome == 'failed':
                        pipe.sadd(self.dead, run_id)
                    pipe.execute()
                    return state
                except WatchError:
                    continue


class RedisQueuedStore(RedisStore):
    """Use another delivery adapter while retaining the atomic storage outbox."""
    def __init__(self, client, namespace, queue):
        super().__init__(client, namespace)
        self.queue = queue

    def receive(self, consumer, cursor='0-0', block_ms=1000):
        message, cursor = super().receive(consumer, cursor, 0)
        if message is not None:
            self.queue.send(message[0], message[1]['run_id'])
        return self.queue.receive(consumer, block_ms), cursor

    def claim(self, message_id, run_id, consumer):
        claim = super().claim(message_id, run_id, consumer)
        if claim is None:
            self.queue.acknowledge(message_id)
        return claim

    def finish(self, claim, state, delay=0):
        committed = super().finish(claim, state, delay)
        if committed:
            self.queue.acknowledge(claim.message_id)
        return committed
