"""PostgreSQL/MySQL adapters through AnySQL, with no reconnect or statement replay."""
from contextlib import contextmanager, suppress
from importlib import import_module
import hashlib
import re
import threading
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit, urlencode
from weakref import WeakSet

from sonicprobe.libs import anysql

from .sql_backend import SQLBackend
from .sql_dialect import PostgreSQLDialect, MySQLDialect
from .sql_server_schema import SCHEMA_VERSION, TABLES, schema

CONNECT_TIMEOUT_SECONDS = 5


class SQLConnection:
    """Small execute/result interface over AnySQL's bound-parameter cursor API."""
    def __init__(self, owner):
        self.owner = owner
        self.results = WeakSet()

    def execute(self, statement, parameters=None):
        cursor = self.owner.cursor()
        try:
            cursor.query(statement, parameters=parameters)
            result = SQLResult(cursor)
            self.results.add(result)
            return result
        except BaseException:
            with suppress(Exception): cursor.close()
            raise

    def executemany(self, statement, rows):
        # AnySQL.querymany does not translate qmark parameters. Use its query
        # method consistently, on this connection's existing transaction.
        for row in rows: self.execute(statement, row).close()

    def close(self):
        for result in list(self.results): result.close()
        self.owner.close()


class SQLResult:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = cursor.lastrowid
        if cursor.description is None: self.close()

    def close(self):
        if self.cursor is not None:
            cursor, self.cursor = self.cursor, None
            cursor.close()

    def fetchone(self):
        try: return self.cursor.fetchone(raw=True)
        finally: self.close()

    def fetchall(self):
        try: return self.cursor.fetchall(raw=True)
        finally: self.close()

    def __iter__(self):
        try:
            while True:
                row = self.cursor.fetchone(raw=True)
                if row is None: return
                yield row
        finally:
            self.close()

    def __del__(self):
        with suppress(Exception): self.close()


def server_uri(spec):
    """Remote connections require explicit CA and server-identity verification."""
    options = spec.get('options', {})
    if type(options) is not dict or not set(options) <= {'tls'}:
        raise ValueError('unsupported_backend_options')
    tls = options.get('tls')
    if 'tls' in options and (type(tls) is not dict or not {'ca'} <= set(tls) <= {'ca','cert','key'}
            or ('cert' in tls) != ('key' in tls)
            or any(type(value) is not str or not value or '\x00' in value or not Path(value).is_absolute()
                   for value in tls.values())):
        raise ValueError('invalid_sql_tls_configuration')
    parsed = urlsplit(spec.get('url', ''))
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if (spec['backend'] not in ('postgresql','mysql') or parsed.scheme != spec['backend']
            or not parsed.hostname
            or not tls and parsed.hostname not in ('127.0.0.1','::1','localhost')
            or tls and spec['backend']=='mysql' and parsed.hostname.lower()=='localhost'
            or not parsed.username or not re.fullmatch(r'/[A-Za-z][A-Za-z0-9_]{0,62}', unquote(parsed.path))
            or parsed.fragment or parameters not in ([], [('mode','rw')], [('mode','rwc')])
            or parsed.port is not None and not 1 <= parsed.port <= 65535):
        raise ValueError('invalid_sql_backend_url')
    # The engine requires writes; never override an explicitly requested RO mode.
    query = 'mode=rw'
    if parsed.scheme == 'mysql': query += '&charset=utf8mb4&use_unicode=1'
    query += '&connect_timeout=' + str(CONNECT_TIMEOUT_SECONDS)
    if tls:
        query += '&' + urlencode([('tls','verify-full')] + [('tls_'+key,tls[key]) for key in ('ca','cert','key') if key in tls])
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,query,''))


class SQLServerBackend(SQLBackend):
    schema_version = SCHEMA_VERSION

    def __init__(self, spec):
        uri = server_uri(spec)
        self._lock, self._local = threading.RLock(), threading.local()
        self._queues = []
        self.client = self
        if spec['backend'] == 'postgresql':
            adapter = import_module('sonicprobe.libs.BackSQL.backpostgresql')
            if getattr(adapter, 'CONNECT_TIMEOUT_SUPPORTED', False) is not True:
                raise ValueError('postgresql_connection_timeout_adapter_required')
            import psycopg2
            self.database_errors = (psycopg2.Error,)
        else:
            import MySQLdb
            self.database_errors = (MySQLdb.Error,)
        self._connection_owner = anysql.connect_by_uri(uri, auto_reconnect=False)
        self.connection = SQLConnection(self._connection_owner)
        try:
            self._configure(spec['backend'])
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def _configure(self, backend):
        raw, c = self._connection_owner.driver_connection, self.connection
        if backend == 'postgresql':
            self.dialect = PostgreSQLDialect()
            raw.autocommit = True
            if (int(c.execute('SHOW server_version_num').fetchone()[0]) < 160000
                    or c.execute('SHOW server_encoding').fetchone()[0] != 'UTF8'
                    or c.execute('SHOW fsync').fetchone()[0] != 'on'):
                raise ValueError('unsupported_sql_server_configuration')
            c.execute('SET search_path TO public')
            c.execute("SET default_transaction_isolation TO 'read committed'")
            c.execute('SET synchronous_commit TO on')
            c.execute("SET lock_timeout TO '5s'")
            c.execute("SET statement_timeout TO '30s'")
        else:
            raw.autocommit(True)
            version = c.execute('SELECT VERSION()').fetchone()[0]
            maria = 'MariaDB' in version
            numbers = tuple(map(int, re.match(r'([0-9]+)\.([0-9]+)\.([0-9]+)',version).groups()))
            if numbers < ((11,4,0) if maria else (8,4,0)):
                raise ValueError('unsupported_sql_server_version')
            self.collation = 'utf8mb4_nopad_bin' if maria else 'utf8mb4_0900_bin'
            self.dialect = MySQLDialect(self.collation)
            c.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,ANSI_QUOTES,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO'")
            c.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
            c.execute("SET SESSION time_zone='+00:00'")
            c.execute('SET SESSION foreign_key_checks=1')
            if maria: c.execute('SET SESSION check_constraint_checks=1')
            else: c.execute('SET SESSION information_schema_stats_expiry=0')
            c.execute('SET SESSION innodb_lock_wait_timeout=5')
            c.execute('SET SESSION lock_wait_timeout=5')
            if c.execute('SELECT @@GLOBAL.innodb_flush_log_at_trx_commit').fetchone()[0] != 1:
                raise ValueError('durable_sql_server_required')

    def _initialize(self):
        c, pg = self.connection, self.dialect.name == 'postgresql'
        if pg:
            c.execute('SELECT pg_advisory_lock(1096045135)').close()
            locked = True
        else:
            self._bootstrap_lock = 'atropos:' + hashlib.sha256(c.execute('SELECT DATABASE()').fetchone()[0].encode()).hexdigest()[:48]
            locked = c.execute('SELECT GET_LOCK(?,5)',(self._bootstrap_lock,)).fetchone()[0]
        if not locked: raise ValueError('storage_initialization_busy')
        try:
            ddl = schema(self.dialect.name, getattr(self,'collation',None))
            fingerprint = hashlib.sha256('\n'.join(ddl).encode()).hexdigest()
            if pg:
                tables = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
                objects = c.execute("SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace LIMIT 1").fetchone()
                functions = c.execute("SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace LIMIT 1").fetchone()
                if not tables and (objects or functions): raise ValueError('unsupported_storage_format')
            else:
                tables = {r[0] for r in c.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()')}
            if not tables:
                if pg: c.execute('BEGIN')
                try:
                    for statement in ddl: c.execute(statement)
                    c.execute('INSERT INTO storage_metadata VALUES(1,?,?)',(self.schema_version,fingerprint))
                    if pg: c.execute('COMMIT')
                except BaseException:
                    if pg:
                        with suppress(Exception): c.execute('ROLLBACK')
                    # MySQL DDL may already be committed. Never auto-delete or
                    # resume such a partial database on the next connection.
                    raise
            elif tables != TABLES:
                raise ValueError('unsupported_storage_format')
            rows = c.execute('SELECT id,version,schema_hash FROM storage_metadata').fetchall()
            if list(rows) != [(1,self.schema_version,fingerprint)]:
                raise ValueError('unsupported_storage_format')
            # Missing mandatory triggers cannot masquerade as a current schema.
            query = ("SELECT trigger_name FROM information_schema.triggers WHERE trigger_schema='public'" if pg
                     else 'SELECT trigger_name FROM information_schema.triggers WHERE trigger_schema=DATABASE()')
            triggers = {'admissions_evidence','admissions_immutable','run_identity_immutable'}
            if not pg: triggers |= {'journal_id_positive','journal_immutable'}
            if {r[0] for r in c.execute(query)} != triggers:
                raise ValueError('unsupported_storage_format')
        finally:
            if pg: c.execute('SELECT pg_advisory_unlock(1096045135)').close()
            else: c.execute('SELECT RELEASE_LOCK(?)',(self._bootstrap_lock,)).close()

    @contextmanager
    def transaction(self):
        with self._lock:
            if getattr(self._local,'active',False):
                yield self
                return
            self.connection.execute('BEGIN')
            self._local.active = True
            try:
                # A shared database row, not a per-process mutex: admission,
                # queue capacity, leases, rules and backup share one domain.
                row = self.connection.execute('SELECT id FROM storage_metadata WHERE id=1 FOR UPDATE').fetchone()
                if row != (1,): raise ValueError('unsupported_storage_format')
                yield self
                # psycopg2 commit()/rollback() are no-ops in autocommit mode,
                # even after an explicit SQL BEGIN. Complete this explicit
                # transaction through the same AnySQL cursor interface.
                self.connection.execute('COMMIT')
            except BaseException:
                # A failed COMMIT may have succeeded on the server. Rollback
                # cleanup never masks that error and never replays the work.
                with suppress(Exception): self.connection.execute('ROLLBACK')
                raise
            finally:
                self._local.active = False

    def journal_sequence(self):
        if self.dialect.name == 'postgresql':
            value, called = self.connection.execute('SELECT last_value,is_called FROM journal_id_seq').fetchone()
            return value if called else 0
        value = self.connection.execute("SELECT auto_increment FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='journal'").fetchone()[0]
        return value - 1

    def now(self):
        query = ('SELECT EXTRACT(EPOCH FROM clock_timestamp())' if self.dialect.name == 'postgresql'
                 else 'SELECT UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))')
        with self._lock:
            return float(self.connection.execute(query).fetchone()[0])

    def restore_sequence(self, value):
        # This is the last restore operation, after all constraints and state
        # checks. Server counters may consume gaps on rollback, never rows.
        if self.dialect.name == 'postgresql':
            self.connection.execute("SELECT setval('journal_id_seq',?,?)",(max(1,value),value>0)).close()
        else:
            maximum = self.connection.execute('SELECT COALESCE(MAX(id),0) FROM journal').fetchone()[0]
            if value > maximum:
                # ALTER TABLE would implicitly COMMIT. A temporary, unlinked
                # journal row advances AUTO_INCREMENT within the data transaction.
                self.connection.execute('INSERT INTO journal(id,"key",value) VALUES(?,?,?)',(value,'restore:sequence',''))
                self.connection.execute('DELETE FROM journal WHERE id=?',(value,))

    def check_integrity(self):
        from .storage_backup import BackupError
        invalid = self.connection.execute('''SELECT 1 FROM admissions a
            LEFT JOIN runs r ON r.namespace=a.namespace AND r.id=a.run_id
            LEFT JOIN journal j ON j.id=a.journal_id WHERE r.id IS NULL OR j.id IS NULL LIMIT 1''').fetchone()
        if invalid: raise BackupError('invalid_backup_relationships')
