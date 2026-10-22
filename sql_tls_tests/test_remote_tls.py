"""Failed TLS authentication must stop storage opening before schema creation."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import MySQLdb
import psycopg2
from atropos.backend_registry import open_storage


class RemoteTLSFailures(unittest.TestCase):
    def setUp(self):
        self.ca = Path(os.environ['ATROPOS_SQL_TEST_TLS_CA'])
        self.assertTrue(self.ca.is_file())

    def rejected(self, *, host='sql-tls.test', offset=0, ca=None):
        for name, backend, port in [('PostgreSQL','postgresql',35432),
                                    ('MySQL','mysql',33306),('MariaDB','mysql',33316)]:
            with self.subTest(engine=name):
                spec = {'backend':backend,
                        'url':f'{backend}://anysql:synthetic-test-password@{host}:{port+offset}/anysql_test',
                        'options':{'tls':{'ca':str(ca or self.ca)}}}
                # If opening unexpectedly succeeds, cleanup still closes it.
                with patch('atropos.sql_server.SQLServerBackend._configure',
                           side_effect=AssertionError('Storage configuration reached before TLS rejection')):
                    with self.assertRaises((MySQLdb.Error, psycopg2.Error, ValueError)) as failure:
                        self.addCleanup(open_storage(spec).close)
                if isinstance(failure.exception, ValueError):
                    self.assertEqual(str(failure.exception), 'SQL TLS was not negotiated')

    def test_wrong_certificate_authority(self):
        self.rejected(ca=self.ca.with_name('wrong-ca.pem'))

    def test_wrong_server_name(self):
        self.rejected(host='wrong.sql-tls.test')

    def test_expired_server_certificate(self):
        self.rejected(offset=2)

    def test_plaintext_server(self):
        self.rejected(offset=1)

    def test_missing_certificate_authority_file(self):
        self.rejected(ca=self.ca.with_name('absent-ca.pem'))
