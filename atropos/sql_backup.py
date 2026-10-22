"""Data-only snapshots of the SQL transaction domain, never executable SQL.

SQL owns the durable outbox. A separate delivery queue is rebuilt from it on
resume; restoration requires fresh delivery namespaces and stopped services.
"""
import hashlib
import json
import time

from .maintenance import PAUSE_SUFFIX
from .sql_maintenance import BACKUP_SUFFIX
from .storage_backup import BackupError, canonical, prefixes, MAX_DATA_BYTES

SCHEMA_VERSIONS = {'sqlite': 2, 'postgresql': 1, 'mysql': 1}
FORMAT = 3
MAX_ROWS = 100000
TABLES = {'records': 3, 'journal': 3, 'runs': 11, 'admissions': 9}
FIELDS = frozenset({'version', 'backend', 'schema', 'namespace', 'captured_at_ms', 'tables', 'journal_sequence'})
SAFE_STATUSES = frozenset({'queued', 'retrying', 'uncertain', 'succeeded', 'failed', 'cancelled'})


def checked(payload):
    if (type(payload) is not dict or set(payload) != FIELDS
            or type(payload['version']) is not int or payload['version'] != FORMAT
            or type(payload['backend']) is not str or payload['backend'] not in SCHEMA_VERSIONS
            or type(payload['schema']) is not int or payload['schema'] != SCHEMA_VERSIONS[payload['backend']]
            or type(payload['captured_at_ms']) is not int or payload['captured_at_ms'] <= 0
            or type(payload['journal_sequence']) is not int or not 0 <= payload['journal_sequence'] < 2**63
            or type(payload['tables']) is not dict or set(payload['tables']) != set(TABLES)):
        raise ValueError('invalid_sql_backup')
    allowed = prefixes(payload['namespace'])
    count = 0
    for table, width in TABLES.items():
        rows = payload['tables'][table]
        if type(rows) is not list: raise ValueError('invalid_sql_backup')
        count += len(rows)
        if count > MAX_ROWS: raise ValueError('backup_limit_exceeded')
        for row in rows:
            if (type(row) is not list or len(row) != width
                    or any(value is not None and type(value) not in (str, int, float) for value in row)):
                raise ValueError('invalid_sql_backup_row')
            key = row[1] if table == 'journal' else row[0]
            if type(key) is not str or (key not in allowed if table in ('runs', 'admissions')
                                       else not key.startswith(tuple(p + ':' for p in allowed))):
                raise ValueError('backup_requires_dedicated_database')
            if table == 'records' and key.endswith(BACKUP_SUFFIX):
                raise ValueError('invalid_backup_operation')
    if len(canonical(payload)) > MAX_DATA_BYTES: raise ValueError('backup_limit_exceeded')
    return payload


def inspect_state(backend, namespace):
    """Check safe checkpoints and retained exclusion inside the snapshot transaction."""
    connection = backend.connection
    allowed = prefixes(namespace)
    rows = connection.execute('SELECT namespace,id,state,status,target,token,lease_until FROM runs')
    for ns, run_id, raw, status, target, token, lease in rows:
        if ns not in allowed or status not in SAFE_STATUSES or token is not None or lease is not None:
            raise BackupError('backup_requires_quiescence')
        state = json.loads(raw)
        target_key = ns + ':target:' + hashlib.sha256(target.encode()).hexdigest()
        lock = connection.execute('SELECT value,expires FROM records WHERE "key"=?', (target_key,)).fetchone()
        if status == 'uncertain' or (status in ('queued', 'retrying') and state.get('attempts')):
            if lock != (run_id, None): raise BackupError('invalid_backup_target_lock')
    for key, value, expires in connection.execute('SELECT "key",value,expires FROM records'):
        for ns in allowed:
            if key.startswith(ns + ':target:'):
                row = connection.execute('SELECT target,status FROM runs WHERE namespace=? AND id=?', (ns, value)).fetchone()
                if (row is None or row[1] not in ('queued', 'retrying', 'uncertain') or expires is not None
                        or key != ns + ':target:' + hashlib.sha256(row[0].encode()).hexdigest()):
                    raise BackupError('invalid_backup_target_lock')
    backend.check_integrity()


def capture(backend, namespace, *, operation=''):
    allowed = prefixes(namespace)
    with backend.transaction():
        if any(backend.get(ns + PAUSE_SUFFIX) is None for ns in allowed):
            raise BackupError('backup_requires_paused_execution')
        if operation and backend.get(namespace + BACKUP_SUFFIX) != operation:
            raise BackupError('backup_operation_changed')
        tables, count, size = {}, 0, 0
        for table in TABLES:
            tables[table] = []
            # Table names are fixed module constants, never archive input.
            for row in backend.connection.execute('SELECT * FROM ' + table + ' ORDER BY 1,2'):
                if table == 'records' and row[0] == namespace + BACKUP_SUFFIX: continue
                count += 1; size += len(canonical(row))
                if count > MAX_ROWS or size > MAX_DATA_BYTES: raise ValueError('backup_limit_exceeded')
                tables[table].append(list(row))
        sequence = backend.journal_sequence()
        payload = dict(version=FORMAT, backend=backend.dialect.name, schema=backend.schema_version, namespace=namespace,
                       captured_at_ms=int(time.time()*1000), tables=tables, journal_sequence=sequence)
        checked(payload)
        inspect_state(backend, namespace)
        return payload


def require_fresh_queue(spec, namespace):
    if spec == {'backend': 'storage'}: return
    if spec.get('backend') != 'redis': raise ValueError('unsupported_backup_queue')
    from .backend_registry import redis_connection
    client = redis_connection(spec)
    try:
        for ns in prefixes(namespace):
            if next(client.scan_iter(match=ns + ':delivery:*', count=100), None) is not None:
                raise BackupError('restore_requires_empty_delivery_queue')
    finally:
        client.close()


def restore(backend, namespace, payload, *, queue):
    checked(payload)
    if payload['backend'] != backend.dialect.name or payload['schema'] != backend.schema_version:
        raise ValueError('restore_backend_mismatch')
    if payload['namespace'] != namespace: raise ValueError('restore_namespace_mismatch')
    require_fresh_queue(queue, namespace)
    try:
        with backend.transaction():
            if any(backend.connection.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone() for table in TABLES):
                raise BackupError('restore_requires_empty_database')
            for table, width in TABLES.items():
                rows = payload['tables'][table]
                # All expirations remain absolute. Expired proofs stay expired.
                backend.connection.executemany('INSERT INTO ' + table + ' VALUES(' + ','.join('?'*width) + ')', rows)
            maximum = backend.connection.execute('SELECT COALESCE(MAX(id),0) FROM journal').fetchone()[0]
            if payload['journal_sequence'] < maximum: raise ValueError('invalid_backup_sequence')
            inspect_state(backend, namespace)
            for ns in prefixes(namespace): backend.set(ns + PAUSE_SUFFIX, '1')
            backend.restore_sequence(payload['journal_sequence'])
            # No network writes or stale consumer state: workers publish the durable
            # outbox to the fresh queue, including delayed work when it becomes due.
    except backend.database_errors:
        raise BackupError('invalid_sql_backup_state') from None
    return dict(summary(payload), execution='paused', admissions='subject_to_capacity_and_authorization',
                delivery='rebuilt_from_durable_outbox')


def summary(payload):
    return dict(version=FORMAT, backend=payload['backend'], schema=payload['schema'], namespace=payload['namespace'],
                rows=sum(len(rows) for rows in payload['tables'].values()), captured_at_ms=payload['captured_at_ms'],
                sha256=hashlib.sha256(canonical(payload)).hexdigest())
