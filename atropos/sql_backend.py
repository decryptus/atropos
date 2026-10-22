"""Shared SQL transaction-domain behavior; connections and DDL belong to adapters."""
import hashlib
import json
import math
import time
import uuid

from .admission import admission_state
from .definitions import encode, name
from .maintenance import PAUSE_SUFFIX
from .storage_contract import Claim, Conflict, QueueFull

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
MAX_STATE_BYTES = 1048576


class SQLBackend:
    """One connection and transaction domain for rules, runs and durable evidence."""
    def now(self):
        return time.time()

    def get(self, key):
        with self._lock:
            row = self.connection.execute('SELECT value FROM records WHERE "key"=? AND (expires IS NULL OR expires>?)', (key,self.now())).fetchone()
            return row[0] if row else None

    def set(self, key, value, ex=None):
        with self.transaction():
            self.connection.execute(self.dialect.record_upsert,
                                    (key,value,self.now()+ex if ex else None))

    def delete(self, *keys):
        with self.transaction():
            self.connection.executemany('DELETE FROM records WHERE "key"=?', [(k,) for k in keys])

    def append(self, key, value):
        with self.transaction():
            result = self.connection.execute(self.dialect.journal_insert, (key,value))
            return result.fetchone()[0] if self.dialect.name == 'postgresql' else result.lastrowid

    def entries(self, key, offset=0, limit=100):
        with self._lock:
            return [r[0] for r in self.connection.execute('SELECT value FROM journal WHERE "key"=? ORDER BY id LIMIT ? OFFSET ?', (key,limit,offset))]

    def workflow_store(self, namespace, queue=None):
        if queue is not None: self._queues.append(queue)
        return SQLWorkflowStore(self, namespace, queue=queue)

    def repository(self, organization, namespace):
        from .sql_rules import SQLRuleRepository
        return SQLRuleRepository(self, organization, namespace)

    def lifecycle_store(self, repository):
        from .sql_rules import SQLLifecycleStore
        return SQLLifecycleStore(repository)

    def maintenance(self, namespace):
        from .sql_maintenance import SQLExecutionMaintenance
        return SQLExecutionMaintenance(self, namespace)

    def capture_backup(self, namespace, *, operation=''):
        from .sql_backup import capture
        return capture(self, namespace, operation=operation)

    def restore_backup(self, namespace, payload):
        from .sql_backup import restore
        return restore(self, namespace, payload, queue=getattr(self, 'backup_queue', {'backend': 'storage'}))

    def close(self):
        for queue in self._queues: queue.close()
        self.connection.close()


class SQLWorkflowStore:
    def __init__(self, backend, namespace='atropos', lease_ms=30000, queue=None):
        name(namespace, 'namespace')
        if type(lease_ms) is not int or lease_ms < 300: raise ValueError('invalid_lease')
        self.client, self.prefix, self.lease_ms = backend, namespace, lease_ms
        self.queue = queue
        self.execution_pause = namespace + PAUSE_SUFFIX

    def intervention_index(self, test_store):
        from .sql_search import SQLInterventionIndex
        return SQLInterventionIndex(self,test_store)

    def now(self): return self.client.now()
    def initialize(self): pass
    def key(self, run_id): return self.prefix + ':run:' + run_id
    def target_key(self, target): return self.prefix + ':target:' + hashlib.sha256(target.encode()).hexdigest()

    def _row(self, run_id):
        return self.client.connection.execute('SELECT state,delivery,token,lease_until FROM runs WHERE namespace=? AND id=?', (self.prefix,run_id)).fetchone()

    def get(self, run_id):
        with self.client._lock:
            row = self._row(run_id)
            if row is None: raise KeyError(run_id)
            return json.loads(row[0])

    def _put(self, state, delivery, token=None, lease_until=None):
        # Bind the same decimal spelling as JSON. SQLite versions differ in
        # decimal-to-binary rounding; a Python binary float can differ by one
        # ULP from json_extract and violate the exact metadata constraint.
        self.client.connection.execute(self.client.dialect.run_upsert,
            (self.prefix,state['id'],encode(state,MAX_STATE_BYTES),state['status'],state['target'],
             state['workflow']['name'],str(state['created_at']),state.get('available_at',0),delivery,token,lease_until))

    def configure_queue(self, max_outstanding):
        if type(max_outstanding) is not int or not 0 <= max_outstanding <= 1000000:
            raise ValueError('invalid_queue_limit')
        self.client.set(self.prefix + ':queue-limit', str(max_outstanding))
        return self.queue_status()

    def queue_status(self):
        with self.client.transaction():
            raw = self.client.get(self.prefix + ':queue-limit')
            if raw is not None and (not raw.isascii() or not raw.isdigit() or len(raw)>7 or int(raw)>1000000):
                raise ValueError('invalid_queue_limit')
            limit = 1000 if raw is None else int(raw)
            counts = dict(self.client.connection.execute('SELECT status,COUNT(*) FROM runs WHERE namespace=? GROUP BY status', (self.prefix,)))
            total = sum(counts.get(s,0) for s in ('queued','retrying','running','uncertain'))
            return dict(namespace=self.prefix,max_outstanding=limit,
                pending_entries=counts.get('queued',0)+counts.get('running',0),delayed_entries=counts.get('retrying',0),
                uncertain_runs=counts.get('uncertain',0),outstanding=total,available=max(0,limit-total),full=total>=limit)

    def enqueue(self, workflow, inputs, target, deduplication_key=None, attribution=None, *, execution=None, conditions=None, audit=None, consume=(), not_after=None, test_context=None):
        state = admission_state(self.now, workflow, inputs, target, deduplication_key, attribution,
            execution=execution,conditions=conditions,audit=audit,consume=consume,not_after=not_after,test_context=test_context)
        with self.client.transaction():
            if any(self.client.get(k)!=v for k,v in (conditions or {}).items()): raise Conflict('admission_precondition_changed')
            if not_after is not None and self.now() >= not_after: raise Conflict('admission_deadline_expired')
            row = self._row(state['id'])
            if row:
                old = json.loads(row[0])
                if old['fingerprint'] != state['fingerprint']: raise Conflict('deduplication_content_changed')
                return old
            if self.queue_status()['full']: raise QueueFull('rule_queue_full')
            self._put(state,uuid.uuid4().hex)
            self.client.delete(*consume)
            if audit:
                journal_id = self.client.append(audit[0],encode(audit[1]))
                if audit[1].get('operation') in ('dispatch','test_run'):
                    event, proof, owner = audit[1], state['execution'], state['attribution']
                    mode = 'test' if event['operation']=='test_run' else 'real'
                    if (event['run_id']!=state['id'] or event['author']!=owner['subject']
                            or event['organization']!=owner['organization'] or event['revision']!=proof['revision']
                            or owner['issuer']!=('atropos.tests' if mode=='test' else 'atropos.rules')
                            or (mode=='test' and (state.get('test_context')!=event.get('test_context') or state.get('test_context')!={'test':True,'id':state['id']}))
                            or (mode=='real' and 'test_context' in state)):
                        raise ValueError('invalid_intervention_evidence')
                    self.client.connection.execute('INSERT INTO admissions VALUES(?,?,?,?,?,?,?,?,?)',
                        (self.prefix,state['id'],journal_id,owner['organization'],owner['subject'],proof['rule'],proof['revision'],proof['event_id'],mode))
        # No network call in admission: the committed run is the durable outbox.
        return state

    def _ready(self, *, paused=False):
        with self.client._lock:
            expired = list(self.client.connection.execute(
                "SELECT delivery,id FROM runs WHERE namespace=? AND status='running' AND lease_until<=? ORDER BY lease_until,id LIMIT 100",
                (self.prefix,self.now())))
            if paused or len(expired)==100: return expired
            queued = list(self.client.connection.execute(
                "SELECT delivery,id FROM runs WHERE namespace=? AND status IN ('queued','retrying') AND available<=? ORDER BY available,created,id LIMIT ?",
                (self.prefix,self.now(),100-len(expired))))
            return expired+queued

    def receive(self, consumer, cursor='0-0', block_ms=1000):
        deadline = time.monotonic() + block_ms/1000
        while True:
            paused = self.client.get(self.execution_pause) is not None
            rows = self._ready(paused=paused)
            if self.queue is not None:
                for delivery,run_id in rows: self.queue.send(delivery,run_id)
                # Bounded polling also flushes newly committed outbox entries.
                message = self.queue.receive(consumer, min(block_ms,100))
                if message: return message,cursor
            elif rows:
                return (rows[0][0],{'run_id':rows[0][1]}),cursor
            if paused or not block_ms or time.monotonic() >= deadline: return None,cursor
            time.sleep(min(.05,max(0,deadline-time.monotonic())))

    def _ack(self, delivery):
        if self.queue is not None: self.queue.acknowledge(delivery)

    def claim(self, message_id, run_id, consumer):
        result = None
        acknowledge = False
        with self.client.transaction():
            row = self._row(run_id)
            if row is None: raise Conflict('queue_references_missing_run')
            state = json.loads(row[0]); now = self.now()
            if row[1] != message_id or state['status'] in TERMINAL or state['status']=='uncertain':
                acknowledge = True
            elif row[3] is not None and row[3] > now:
                return None
            elif state['status']=='running':
                state.update(status='uncertain',error='worker lease expired; action outcome requires reconciliation')
                state['history'].append(dict(step=state['current'],status='uncertain',at=now,
                    **({'test_context':state['test_context']} if 'test_context' in state else {})))
                self._put(state,message_id)
                acknowledge = True
            elif self.client.get(self.execution_pause) is not None:
                return None
            elif state.get('available_at',0)>now:
                return None
            else:
                target = self.target_key(state['target']); owner = self.client.get(target)
                if owner is not None and owner != run_id:
                    state['available_at']=now+.5
                    self._put(state,uuid.uuid4().hex)
                    acknowledge = True
                else:
                    state['status']='running'; step=state['current']
                    state['attempts'][step]=state['attempts'].get(step,0)+1
                    state['history'].append(dict(step=step,status='running',at=now,
                        **({'test_context':state['test_context']} if 'test_context' in state else {})))
                    token=uuid.uuid4().hex
                    self.client.set(target,run_id)
                    self._put(state,message_id,token,now+self.lease_ms/1000)
                    result=Claim(state,token,message_id,consumer)
        if acknowledge: self._ack(message_id)
        return result

    def heartbeat(self, claim):
        with self.client.transaction():
            row=self._row(claim.state['id'])
            if not row or row[2]!=claim.token or row[3] is None or row[3]<=self.now(): return False
            self.client.connection.execute('UPDATE runs SET lease_until=? WHERE namespace=? AND id=?',
                (self.now()+self.lease_ms/1000,self.prefix,claim.state['id']))
            return True

    def finish(self, claim, state, delay=0):
        if type(delay) not in (int,float) or not math.isfinite(delay) or delay<0: raise ValueError('invalid_delay')
        with self.client.transaction():
            row=self._row(claim.state['id'])
            if (state['id']!=claim.state['id'] or state['target']!=claim.state['target'] or not row
                    or row[2]!=claim.token or row[3] is None or row[3]<=self.now()
                    or self.client.get(self.target_key(state['target']))!=state['id']): return False
            state['updated_at']=self.now(); state['available_at']=state['updated_at']+delay
            self._put(state,uuid.uuid4().hex)
            if state['status'] in TERMINAL: self.client.delete(self.target_key(state['target']))
        self._ack(claim.message_id)
        return True

    def reconcile(self, run_id, outcome, *, expected_state, reconciliation, audit):
        from .reconciliation import transition
        with self.client.transaction():
            row = self._row(run_id)
            if not row:
                raise KeyError(run_id)
            state = json.loads(row[0])
            target = self.target_key(state['target'])
            if row[3] is not None and row[3] > self.now():
                raise Conflict('run_has_active_lease')
            if self.client.get(target) != run_id:
                raise Conflict('target_ownership_changed')
            result = transition(state, outcome, expected_state, reconciliation)
            self._put(result, uuid.uuid4().hex)
            self.client.append(audit[0], encode(audit[1]))
            self.client.delete(target)
            return result

    def resolve(self, run_id, outcome):
        if outcome not in ('succeeded','failed'): raise ValueError('invalid_resolution')
        with self.client.transaction():
            row=self._row(run_id)
            if not row: raise KeyError(run_id)
            state=json.loads(row[0]);target=self.target_key(state['target'])
            if state['status']!='uncertain' or row[3] and row[3]>self.now(): raise Conflict('run_not_reconcilable')
            if self.client.get(target)!=run_id: raise Conflict('target_ownership_changed')
            state.update(status=outcome,resolution='operator-confirmed terminal outcome',updated_at=self.now())
            self._put(state,uuid.uuid4().hex);self.client.delete(target)
            return state
