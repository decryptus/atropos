"""Bounded, atomic snapshots of a dedicated execution-paused standalone database.

Trusted host administration only. No HTTP route, shell command, FLUSHDB or
replacement of existing destination keys. Restores always pause execution and preserve admission limits.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat

from .definitions import name
from .test_execution import test_namespace

class BackupError(ValueError):
    """Safe fixed operator-facing error; never a storage exception body."""


ERRORS = {
    'backup_requires_paused_execution': 'Pause both execution queues before taking a backup.',
    'backup_requires_quiescence': 'Wait for in-flight steps and leases to finish before taking a backup; queued and uncertain runs may remain.',
    'backup_requires_dedicated_database': 'Backup requires a database dedicated to this engine namespace and its test namespace.',
    'backup_limit_exceeded': 'Backup exceeds the supported size or key limit.',
    'restore_requires_empty_database': 'Restore requires a completely empty destination database; nothing was replaced.',
    'invalid_backup_dump': 'Backup contains an invalid storage dump; restored keys were removed.',
    'invalid_backup_state': 'Backup contains in-flight or invalid state; restored keys were removed.',
    'backup_operation_changed': 'Backup operation ownership changed.',
    'invalid_backup_target_lock': 'Backup target exclusion or uncertainty index is inconsistent.',
}


def script(client, source, *args):
    from redis.exceptions import ResponseError
    try:
        return client.eval(source, 0, *args)
    except ResponseError as error:
        message = ERRORS.get(str(error))
        if message:
            raise BackupError(message) from None
        raise


FORMAT = 2
MAX_KEYS = 10000
MAX_DATA_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 48 * 1024 * 1024
PAYLOAD_FIELDS = frozenset({'version', 'namespace', 'captured_at_ms', 'redis_major', 'entries'})
ENTRY_FIELDS = frozenset({'key', 'dump', 'expires_at_ms'})

# This validation runs inside the same script as snapshot/restore. A concurrent
# producer cannot insert an unobserved key between enumeration and serialization.
QUIESCENCE = """
local function inspect(keys, first, second)
  local locks = {}
  for _, key in ipairs(keys) do
    local prefix = nil
    if string.sub(key, 1, #first + 1) == first .. ':' then prefix = first end
    if string.sub(key, 1, #second + 1) == second .. ':' then prefix = second end
    if not prefix then return 'backup_requires_dedicated_database' end
    local tail = string.sub(key, #prefix + 2)
    if string.sub(tail, 1, 7) == 'target:' then
      local owner = redis.call('GET', key)
      local suffix = string.sub(tail, 8)
      if #suffix ~= 64 or string.find(suffix, '[^0-9a-f]')
          or redis.call('PTTL', key) ~= -1 then return 'invalid_backup_target_lock' end
      local runkey = prefix .. ':run:' .. owner
      if locks[runkey] then return 'invalid_backup_target_lock' end
      local raw = redis.call('GET', runkey)
      local ok, state = pcall(cjson.decode, raw or '')
      if ok and state.status == 'running' then return 'backup_requires_quiescence' end
      if not ok or state.id ~= owner or (state.status ~= 'queued'
          and state.status ~= 'retrying' and state.status ~= 'uncertain') then
        return 'invalid_backup_target_lock'
      end
      locks[runkey] = true
    end
    if string.sub(tail, 1, 6) == 'lease:' then return 'backup_requires_quiescence' end
    if string.sub(tail, 1, 4) == 'run:' then
      local ok, value = pcall(cjson.decode, redis.call('GET', key))
      if not ok or (value.status ~= 'succeeded' and value.status ~= 'failed'
          and value.status ~= 'cancelled' and value.status ~= 'uncertain'
          and value.status ~= 'queued' and value.status ~= 'retrying') then
        return 'backup_requires_quiescence'
      end
    end
  end
  for _, prefix in ipairs({first, second}) do
    if redis.call('SCARD', prefix .. ':uncertain') > #keys then
      return 'invalid_backup_target_lock'
    end
    for _, id in ipairs(redis.call('SMEMBERS', prefix .. ':uncertain')) do
      local raw = redis.call('GET', prefix .. ':run:' .. id)
      local ok, state = pcall(cjson.decode, raw or '')
      if not ok or state.id ~= id or state.status ~= 'uncertain' then
        return 'invalid_backup_target_lock'
      end
    end
    -- Pending and delayed jobs are safe checkpoints, but every reference must
    -- resolve to a durable queued/retrying run. Never archive orphaned work.
    local references = {}
    if redis.call('EXISTS', prefix .. ':queue') == 1 then
      for _, entry in ipairs(redis.call('XRANGE', prefix .. ':queue', '-', '+')) do
        local fields = entry[2]
        if #fields ~= 2 or fields[1] ~= 'run_id' then return 'backup_requires_quiescence' end
        table.insert(references, fields[2])
      end
    end
    if redis.call('EXISTS', prefix .. ':delayed') == 1 then
      for _, id in ipairs(redis.call('ZRANGE', prefix .. ':delayed', 0, -1)) do
        table.insert(references, id)
      end
    end
    local seen = {}
    for _, id in ipairs(references) do
      if seen[id] then return 'backup_requires_quiescence' end
      seen[id] = true
      local raw = redis.call('GET', prefix .. ':run:' .. id)
      if not raw then return 'backup_requires_quiescence' end
      local ok, value = pcall(cjson.decode, raw)
      if not ok or value.id ~= id or (value.status ~= 'queued' and value.status ~= 'retrying') then
        return 'backup_requires_quiescence'
      end
    end
    for _, key in ipairs(keys) do
      if string.sub(key, 1, #prefix + 5) == prefix .. ':run:' then
        local value = cjson.decode(redis.call('GET', key))
        if value.status == 'uncertain' then
          if not locks[key] or redis.call('SISMEMBER', prefix .. ':uncertain', value.id) ~= 1 then
            return 'invalid_backup_target_lock'
          end
        elseif (value.status == 'queued' or value.status == 'retrying')
            and type(value.attempts) == 'table' and next(value.attempts) ~= nil and not locks[key] then
          return 'invalid_backup_target_lock'
        end
        if (value.status == 'queued' or value.status == 'retrying') and not seen[value.id] then
          return 'backup_requires_quiescence'
        end
      end
    end
  end
  return nil
end
"""
SNAPSHOT = QUIESCENCE + """
if redis.call('DBSIZE') > tonumber(ARGV[3]) then return redis.error_reply('backup_limit_exceeded') end
for _, prefix in ipairs({ARGV[1], ARGV[2]}) do
  if redis.call('EXISTS', prefix .. ':execution-paused') ~= 1 then
    return redis.error_reply('backup_requires_paused_execution')
  end
end
if ARGV[5] ~= '' and redis.call('GET', ARGV[1] .. ':backup-operation') ~= ARGV[5] then
  return redis.error_reply('backup_operation_changed')
end
local keys = redis.call('KEYS', '*')
local issue = inspect(keys, ARGV[1], ARGV[2])
if issue then return redis.error_reply(issue) end
local now = redis.call('TIME')
local result = {now[1], now[2]}
local total = 0
for _, key in ipairs(keys) do
  local value = redis.call('DUMP', key)
  local ttl = redis.call('PTTL', key)
  if value and ttl ~= -2 and key ~= ARGV[1] .. ':backup-operation' then
    total = total + #key + #value
    if total > tonumber(ARGV[4]) then return redis.error_reply('backup_limit_exceeded') end
    table.insert(result, key)
    table.insert(result, value)
    table.insert(result, ttl)
  end
end
return result
"""
RESTORE = QUIESCENCE + """
if redis.call('DBSIZE') ~= 0 then return redis.error_reply('restore_requires_empty_database') end
local restored = {}
local function fail(message)
  for _, key in ipairs(restored) do redis.call('DEL', key) end
  return redis.error_reply(message)
end
local now = redis.call('TIME')
local nowms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
for i = 3, #ARGV, 3 do
  local expiry = tonumber(ARGV[i + 1])
  if expiry == 0 or expiry > nowms then
    local result = redis.pcall('RESTORE', ARGV[i], expiry, ARGV[i + 2], 'ABSTTL')
    if type(result) == 'table' and result.err then return fail('invalid_backup_dump') end
    table.insert(restored, ARGV[i])
  end
end
local checked, issue = pcall(inspect, restored, ARGV[1], ARGV[2])
if not checked or issue then return fail('invalid_backup_state') end
redis.call('DEL', ARGV[1] .. ':backup-operation')
redis.call('SET', ARGV[1] .. ':execution-paused', '1')
redis.call('SET', ARGV[2] .. ':execution-paused', '1')
return #restored
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def prefixes(namespace):
    name(namespace, 'namespace')
    return namespace, test_namespace(namespace)


def server_major(client):
    if client.info('cluster').get('cluster_enabled') != 0:
        raise ValueError('backup_requires_standalone_storage')
    return int(client.info('server')['redis_version'].split('.')[0])


def capture(client, namespace, *, operation=""):
    if callable(getattr(client, 'capture_backup', None)):
        return client.capture_backup(namespace, operation=operation)
    first, second = prefixes(namespace)
    major = server_major(client)
    rows = script(client, SNAPSHOT, first, second, MAX_KEYS, MAX_DATA_BYTES, operation)
    timestamp = int(rows[0]) * 1000 + int(rows[1]) // 1000
    entries = []
    for index in range(2, len(rows), 3):
        key, dump, ttl = rows[index:index+3]
        entries.append({'key': key.decode(), 'dump': base64.b64encode(dump).decode(),
                        'expires_at_ms': timestamp + ttl if ttl >= 0 else 0})
    entries.sort(key=lambda entry: entry['key'])
    return {'version': FORMAT, 'namespace': namespace, 'captured_at_ms': timestamp,
            'redis_major': major, 'entries': entries}


def checked(payload):
    if type(payload) is dict and payload.get('backend') in ('sqlite', 'postgresql', 'mysql'):
        from .sql_backup import checked as checked_sql
        return checked_sql(payload)
    if (type(payload) is not dict or set(payload) != PAYLOAD_FIELDS
            or type(payload['version']) is not int or payload['version'] != FORMAT
            or type(payload['captured_at_ms']) is not int or payload['captured_at_ms'] <= 0
            or type(payload['redis_major']) is not int or payload['redis_major'] < 6
            or type(payload['entries']) is not list or len(payload['entries']) > MAX_KEYS):
        raise ValueError('invalid_backup')
    allowed = tuple(prefix + ':' for prefix in prefixes(payload['namespace']))
    decoded, seen, size = [], set(), 0
    for entry in payload['entries']:
        if (type(entry) is not dict or set(entry) != ENTRY_FIELDS
                or type(entry['key']) is not str or not entry['key'].startswith(allowed)
                or len(entry['key'].encode()) > 4096 or entry['key'] in seen
                or type(entry['dump']) is not str or type(entry['expires_at_ms']) is not int
                or not 0 <= entry['expires_at_ms'] <= 253402300799000):
            raise ValueError('invalid_backup_entry')
        data = base64.b64decode(entry['dump'], validate=True)
        size += len(data) + len(entry['key'].encode())
        if not data or size > MAX_DATA_BYTES:
            raise ValueError('backup_limit_exceeded')
        seen.add(entry['key'])
        decoded.append((entry['key'], entry['expires_at_ms'], data))
    return decoded


def write_backup(payload, path):
    checked(payload)
    raw = canonical({'sha256': hashlib.sha256(canonical(payload)).hexdigest(), 'payload': payload})
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('backup_limit_exceeded')
    path = Path(path)
    parent = path.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in (0, os.getuid())
            or stat.S_IMODE(parent.st_mode) & 0o077):
        raise ValueError('backup_requires_private_directory')
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
    return summary(payload)


def read_backup(path):
    from atropos_client.files import read_private
    return decode_backup(read_private(path, MAX_FILE_BYTES))


def decode_backup(raw):
    if len(raw) > MAX_FILE_BYTES: raise ValueError('backup_limit_exceeded')
    outer = json.loads(raw)
    if (type(outer) is not dict or set(outer) != {'sha256', 'payload'}
            or outer['sha256'] != hashlib.sha256(canonical(outer['payload'])).hexdigest()):
        raise ValueError('backup_integrity_failed')
    checked(outer['payload'])
    return outer['payload']


def summary(payload):
    if payload.get('backend') in ('sqlite', 'postgresql', 'mysql'):
        from .sql_backup import summary as summary_sql
        return summary_sql(payload)
    return {'version': FORMAT, 'namespace': payload['namespace'], 'keys': len(payload['entries']),
            'captured_at_ms': payload['captured_at_ms'], 'redis_major': payload['redis_major'],
            'sha256': hashlib.sha256(canonical(payload)).hexdigest()}


def restore(client, namespace, payload, *, source_stopped=False):
    if source_stopped is not True:
        raise ValueError('source_stop_confirmation_required')
    if callable(getattr(client, 'restore_backup', None)):
        return client.restore_backup(namespace, payload)
    if payload.get('backend') in ('sqlite', 'postgresql', 'mysql'):
        raise ValueError('restore_backend_mismatch')
    decoded = checked(payload)
    if namespace != payload['namespace'] or server_major(client) != payload['redis_major']:
        raise ValueError('restore_namespace_or_storage_version_mismatch')
    args = list(prefixes(namespace))
    for key, expiry, data in decoded:
        args.extend((key, expiry, data))
    count = script(client, RESTORE, *args)
    return dict(summary(payload), restored_keys=count, execution='paused', admissions='subject_to_capacity_and_authorization')


@contextmanager
def configured_storage(path):
    from .rule_operator import load_operator_config
    document, _ = load_operator_config(path, remote=True)
    if document['storage']['backend'] in ('sqlite', 'postgresql', 'mysql'):
        from .backend_registry import open_storage
        if document['queue']['backend'] not in ('storage', 'redis'):
            raise BackupError('Configured delivery adapter does not support backup recovery.')
        backend = open_storage(document['storage'])
        backend.backup_queue = document['queue']
        try: yield backend, document['namespace']
        finally: backend.close()
        return
    import redis
    if document['storage']['backend'] != 'redis' or document['queue'] != {'backend': 'storage'}:
        raise BackupError('Configured adapter does not support this Redis snapshot format.')
    url = document['storage']['url']
    client = redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=3, socket_timeout=15,
        **({'ssl_cert_reqs': 'required', 'ssl_check_hostname': True} if url.startswith('rediss://') else {}))
    try: yield client, document['namespace']
    finally: client.close()


def admission_limits(client, namespace, normal=0, tests=0):
    from .redis_store import queue_limit
    queue_limit(normal); queue_limit(tests)
    first, second = prefixes(namespace)
    with client.pipeline(transaction=True) as pipe:
        pipe.set(first + ':queue-limit', str(normal))
        pipe.set(second + ':queue-limit', str(tests))
        pipe.execute()
    return {'namespace': namespace, 'max_outstanding': normal, 'test_max_outstanding': tests,
            'admissions': 'paused' if normal == tests == 0 else 'configured'}


def backup(client, namespace, path):
    """Fence execution/resume through capture and durable file publication.

    Failure releases only our operation fence; execution remains paused. If the
    administrator process dies, explicit recovery of its operation is required.
    """
    from .redis_maintenance import RedisExecutionMaintenance
    control = (client.maintenance(namespace) if callable(getattr(client, 'capture_backup', None))
               else RedisExecutionMaintenance(client, namespace))
    operation = control.begin_backup()
    try:
        return write_backup(capture(client, namespace, operation=operation), path)
    finally:
        control.end_backup(operation)
