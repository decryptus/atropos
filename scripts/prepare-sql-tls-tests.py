"""Create test databases only in the fixed disposable AnySQL TLS containers."""
import os
from pathlib import Path
from urllib.parse import urlencode

from sonicprobe.libs import anysql

ca = Path(os.environ['ATROPOS_SQL_TEST_TLS_CA'])
if not ca.is_absolute() or not ca.is_file():
    raise RuntimeError('An explicit fixture CA file is required')
for backend, port in [('postgresql',35432),('mysql',33306),('mysql',33316)]:
    uri = (f'{backend}://anysql:synthetic-test-password@sql-tls.test:{port}/anysql_test?'
           + urlencode({'tls':'verify-full','tls_ca':str(ca)}))
    owner = anysql.connect_by_uri(uri, auto_reconnect=False)
    try:
        raw = owner.driver_connection
        if backend == 'postgresql': raw.autocommit = True
        else: raw.autocommit(True)
        cursor = owner.cursor()
        try:
            # No IF NOT EXISTS: a pre-existing destination is a fixture error.
            cursor.query('CREATE DATABASE atropos_test' + (
                " ENCODING 'UTF8' TEMPLATE template0" if backend=='postgresql' else ' CHARACTER SET utf8mb4'))
        finally: cursor.close()
    finally: owner.close()
