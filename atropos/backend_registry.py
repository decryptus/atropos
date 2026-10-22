"""Role-based adapters; third parties register factories via Python entry points."""
from importlib.metadata import entry_points
from urllib.parse import urlsplit


def configuration(spec):
    if (type(spec) is not dict or not {'backend'} <= set(spec)
            or not set(spec) <= {'backend', 'url', 'options'}
            or type(spec['backend']) is not str or not spec['backend']
            or 'url' in spec and (type(spec['url']) is not str or not spec['url'].strip())
            or type(spec.get('options', {})) is not dict):
        raise ValueError('invalid_backend_configuration')
    return dict(spec)


def validate_redis(spec):
    configuration(spec)
    if spec.get('options'):
        raise ValueError('unsupported_backend_options')
    parsed = urlsplit(spec.get('url', ''))
    if (parsed.scheme not in ('redis', 'rediss') or not parsed.hostname
            or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') and not parsed.path[1:].isdigit()
            or parsed.scheme == 'redis' and parsed.hostname not in ('127.0.0.1', '::1', 'localhost')):
        raise ValueError('invalid_backend_url')
    return parsed


def redis_connection(spec):
    import redis
    parsed = validate_redis(spec)
    return redis.Redis.from_url(spec['url'], decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3,
        **({'ssl_cert_reqs': 'required', 'ssl_check_hostname': True} if parsed.scheme == 'rediss' else {}))


class RedisBackend:
    def __init__(self, client):
        self.client = client
        self.queues = []

    def workflow_store(self, namespace, queue=None):
        from .redis_store import RedisStore, RedisQueuedStore
        if queue is not None:
            self.queues.append(queue)
            return RedisQueuedStore(self.client, namespace, queue)
        return RedisStore(self.client, namespace)

    def repository(self, organization, namespace):
        from .rule_repository import RedisRuleRepository
        return RedisRuleRepository(self.client, organization, namespace)

    def lifecycle_store(self, repository):
        from .rule_lifecycle_store import RedisLifecycleStore
        return RedisLifecycleStore(repository)

    def maintenance(self, namespace):
        from .redis_maintenance import RedisExecutionMaintenance
        return RedisExecutionMaintenance(self.client, namespace)

    def close(self):
        for queue in self.queues: queue.close()
        self.client.close()


def _factory(role, name):
    matches = list(entry_points(group='atropos.' + role + '_backends', name=name))
    if len(matches) != 1:
        raise ValueError('unsupported_or_ambiguous_' + role + '_backend')
    return matches[0].load()


def open_storage(spec):
    spec = configuration(spec)
    if spec['backend'] == 'redis':
        return RedisBackend(redis_connection(spec))
    if spec['backend'] == 'sqlite':
        from .sqlite_backend import SQLiteBackend
        return SQLiteBackend(spec)
    if spec['backend'] in ('postgresql', 'mysql'):
        from .sql_server import SQLServerBackend
        return SQLServerBackend(spec)
    return _factory('storage', spec['backend'])(spec)


def open_queue(spec, namespace, lease_ms=30000):
    spec = configuration(spec)
    if spec['backend'] == 'storage':
        if set(spec) != {'backend'}:
            raise ValueError('integrated_queue_has_no_separate_configuration')
        return None
    if spec['backend'] == 'redis':
        from .delivery_queue import RedisDeliveryQueue
        return RedisDeliveryQueue(redis_connection(spec), namespace, lease_ms)
    return _factory('queue', spec['backend'])(spec, namespace=namespace, lease_ms=lease_ms)


def destination(document):
    return document['storage'], document.get('queue', {'backend': 'storage'}), document['namespace']


def validate_configuration(role, spec):
    spec = configuration(spec)
    backend = spec['backend']
    if backend == 'redis':
        validate_redis(spec)
    elif backend == 'storage' and role == 'queue':
        if set(spec) != {'backend'}: raise ValueError('invalid_integrated_queue_configuration')
    elif backend == 'sqlite' and role == 'storage':
        from .sqlite_backend import storage_path
        storage_path(spec)
    elif backend in ('postgresql', 'mysql') and role == 'storage':
        from .sql_server import server_uri
        server_uri(spec)
    else:
        _factory(role, backend)  # Installed adapters only; never an arbitrary import path.
    return spec


def backend_errors():
    import sqlite3
    errors = [sqlite3.Error]
    try:
        import redis
        errors.append(redis.RedisError)
    except ImportError:
        pass
    for module in ('psycopg2', 'MySQLdb'):
        try:
            from importlib import import_module
            errors.append(import_module(module).Error)
        except ImportError:
            pass
    return tuple(errors)
