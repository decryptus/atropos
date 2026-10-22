"""Disposable databases only. Never drop any pre-existing database."""
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit, urlunsplit
import uuid

from sonicprobe.libs import anysql
from atropos.backend_registry import open_storage
from atropos.sql_server import SQLConnection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))


class ServerFixture(unittest.TestCase):
    def setUp(self):
        url = os.environ.get('ATROPOS_SQL_TEST_URL','')
        self.parts = urlsplit(url)
        if (self.parts.scheme not in ('postgresql','mysql') or self.parts.hostname != '127.0.0.1'
                or self.parts.path != '/atropos_test' or self.parts.query or self.parts.fragment):
            raise RuntimeError('A disposable localhost /atropos_test admin URL is required')
        self.tls_ca = os.environ.get('ATROPOS_SQL_TEST_TLS_CA')
        if self.tls_ca:
            from urllib.parse import urlencode
            url=url.replace('@127.0.0.1:', '@sql-tls.test:')+'?'+urlencode({'tls':'verify-full','tls_ca':self.tls_ca})
        self.admin_owner = anysql.connect_by_uri(url, auto_reconnect=False)
        raw = self.admin_owner.driver_connection
        if self.parts.scheme == 'postgresql': raw.autocommit = True
        else: raw.autocommit(True)
        self.admin = SQLConnection(self.admin_owner)
        self.addCleanup(self.admin.close)
        self.spec = self.database_spec()
        self.backend = open_storage(self.spec)
        self.addCleanup(self.backend.close)

    def database_spec(self):
        database = 'atropos_test_' + uuid.uuid4().hex
        # Entire identifier is a trusted, generated ASCII constant. DROP cleanup
        # is registered only after CREATE succeeds, never for an existing DB.
        self.admin.execute('CREATE DATABASE ' + database + (" ENCODING 'UTF8' TEMPLATE template0" if self.parts.scheme=='postgresql' else ' CHARACTER SET utf8mb4'))
        self.addCleanup(self.admin.execute, 'DROP DATABASE ' + database)
        url=urlunsplit(self.parts._replace(path='/'+database))
        if self.tls_ca:
            return dict(backend=self.parts.scheme,url=url.replace('@127.0.0.1:', '@sql-tls.test:'),options={'tls':{'ca':self.tls_ca}})
        return dict(backend=self.parts.scheme, url=url)

    def other_backend(self):
        backend = open_storage(self.database_spec())
        self.addCleanup(backend.close)
        return backend
