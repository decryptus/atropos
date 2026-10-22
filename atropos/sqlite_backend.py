"""Native SQLite adapter with durable outbox, fenced leases and indexed runs.

Each mutation uses one BEGIN IMMEDIATE transaction. Queue delivery can fail or
repeat without losing admission evidence or granting duplicate execution.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from sonicprobe.libs import anysql
from sonicprobe.libs.sqlite_transactions import SQLiteTransactions
from sonicprobe.libs.urisup import uri_help_unsplit
from .sqlite_schema import SCHEMA, SCHEMA_VERSION, APPLICATION_ID
from urllib.parse import unquote, urlsplit

from .sql_backend import SQLBackend
from .sql_dialect import SQLiteDialect


def storage_path(spec):
    if spec.get('options'): raise ValueError('unsupported_backend_options')
    url = spec.get('url', '')
    parsed = urlsplit(url)
    if not url.startswith('sqlite:///') or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('invalid_backend_url')
    path = unquote(url[len('sqlite:///'):])
    if not path or path == ':memory:' or '\x00' in path: raise ValueError('durable_storage_file_required')
    return str(Path(path).absolute())


class SQLiteBackend(SQLBackend):
    dialect = SQLiteDialect()
    schema_version = SCHEMA_VERSION
    database_errors = (sqlite3.Error,)

    def __init__(self, spec):
        self.path = storage_path(spec)
        # New files are private. Existing files and contents are never replaced.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        metadata = os.lstat(self.path)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_nlink != 1):
            raise ValueError('storage_file_not_private')
        uri = uri_help_unsplit(('sqlite3',None,self.path,[('mode','rw'),('timeout_ms','5000'),('check_same_thread','false')],None))
        self._connection_owner = anysql.connect_by_uri(uri,auto_reconnect=False)
        self.connection = self._connection_owner.driver_connection
        self.connection.isolation_level = None
        self._transactions = SQLiteTransactions(self._connection_owner)
        self._lock = self._transactions.lock
        self.connection.create_function('casefold',1,str.casefold,deterministic=True)
        self.connection.execute('PRAGMA foreign_keys=ON')
        try:
            self.connection.execute('PRAGMA synchronous=FULL')
            # Opening an initialized WAL database is a read. A write reservation
            # here can starve behind search requests before any operation starts.
            with self.read_snapshot():
                version = self._schema_version()
            if version == 0:
                with self.transaction():
                    # Another initializer may have committed while we waited.
                    if self._schema_version() == 0:
                        for statement in SCHEMA: self.connection.execute(statement)
                        self.connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
                        self.connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            if self.connection.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                self.connection.execute('PRAGMA journal_mode=WAL')
        except BaseException:
            self.connection.close()
            raise
        # Evidence and rules must share this exact transaction domain.
        self.client = self
        self._queues = []

    def _schema_version(self):
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        application = self.connection.execute('PRAGMA application_id').fetchone()[0]
        tables = self.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if (version not in (0,SCHEMA_VERSION) or (version == 0 and tables)
                or (version == SCHEMA_VERSION and application != APPLICATION_ID)):
            raise ValueError('unsupported_storage_format')
        return version

    @contextmanager
    def transaction(self):
        with self._transactions.transaction():
            yield self

    @contextmanager
    def read_snapshot(self):
        """Stable WAL view; connection-local temporary search tables may change."""
        with self._transactions.read_snapshot():
            yield self

    def journal_sequence(self):
        row = self.connection.execute("SELECT seq FROM sqlite_sequence WHERE name='journal'").fetchone()
        return row[0] if row else 0

    def restore_sequence(self, value):
        self.connection.execute("DELETE FROM sqlite_sequence WHERE name='journal'")
        self.connection.execute("INSERT INTO sqlite_sequence VALUES('journal',?)", (value,))

    def check_integrity(self):
        from .storage_backup import BackupError
        if self.connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise BackupError('invalid_backup_relationships')
        if self.connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise BackupError('invalid_backup_integrity')
