"""Server configuration fails before connecting when semantics cannot be honored."""
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from atropos.backend_registry import validate_configuration, open_storage
from atropos.cli import main
from atropos.sql_server import server_uri


class SQLConfigurationTests(unittest.TestCase):
    def test_configuration_selects_canonical_server_uri(self):
        for backend in ('postgresql','mysql'):
            for suffix in ('','?mode=rw','?mode=rwc'):
                spec=dict(backend=backend,url=backend+'://user:synthetic@127.0.0.1/engine'+suffix)
                self.assertEqual(validate_configuration('storage',spec),spec)
                uri=server_uri(spec)
                self.assertIn('mode=rw',uri)
                self.assertIn('connect_timeout=5',uri)
                self.assertTrue(uri.startswith(backend+'://'))

    def test_older_postgresql_adapter_is_rejected_before_connection(self):
        from types import SimpleNamespace
        for adapter in (SimpleNamespace(), SimpleNamespace(CONNECT_TIMEOUT_SUPPORTED=False)):
            with patch('atropos.sql_server.import_module', return_value=adapter), \
                    patch('atropos.sql_server.anysql.connect_by_uri') as connect:
                with self.assertRaisesRegex(ValueError, 'postgresql_connection_timeout_adapter_required'):
                    open_storage(dict(backend='postgresql',url='postgresql://user:synthetic@127.0.0.1/engine'))
                connect.assert_not_called()

    def test_ro_remote_and_unhandled_options_are_rejected_before_connect(self):
        for backend in ('postgresql','mysql'):
            base=backend+'://user:SECRET_MARKER@127.0.0.1/engine'
            bad=[base+'?mode=ro',base+'?sslmode=verify-full',base+'?mode=rw&mode=ro',
                 base+'?mode=rw&connect_timeout=5',base+'#fragment',base.replace('127.0.0.1','remote.example'),
                 base.replace('/engine','/engine/other'),base.replace('/engine','/engine%00')]
            for url in bad:
                with self.subTest(backend=backend,url=url),patch('atropos.sql_server.anysql.connect_by_uri') as connect,self.assertRaises(ValueError):
                    open_storage(dict(backend=backend,url=url))
                connect.assert_not_called()
            with self.assertRaisesRegex(ValueError,'unsupported_backend_options'):
                server_uri(dict(backend=backend,url=base,options={'sslmode':'verify-full'}))

    def test_invalid_uri_does_not_expose_credentials_in_cli(self):
        output=io.StringIO()
        with patch.dict('os.environ',{'ATROPOS_STORAGE_BACKEND':'postgresql',
                'ATROPOS_STORAGE_URL':'postgresql://user:SECRET_MARKER@remote.example/engine'},clear=True),redirect_stderr(output):
            self.assertEqual(main(['queue-status']),2)
        self.assertNotIn('SECRET_MARKER',output.getvalue())

    def test_remote_tls_maps_to_verified_anysql_parameters(self):
        from urllib.parse import parse_qs, urlsplit
        for backend in ('postgresql','mysql'):
            spec=dict(backend=backend,url=backend+'://engine:synthetic@db.example/engine',
                options={'tls':{'ca':'/certs/ca & root.pem','cert':'/private/client.pem','key':'/private/client.key'}})
            self.assertEqual(validate_configuration('storage',spec),spec)
            parameters=parse_qs(urlsplit(server_uri(spec)).query)
            self.assertEqual(parameters['tls'],['verify-full'])
            self.assertEqual(parameters['tls_ca'],['/certs/ca & root.pem'])
            self.assertEqual(parameters['tls_cert'],['/private/client.pem'])
            self.assertEqual(parameters['tls_key'],['/private/client.key'])

    def test_invalid_tls_options_fail_before_any_connection(self):
        for backend in ('postgresql','mysql'):
            for tls in (None,False,{},[],{'ca':'relative.pem'},{'ca':''},{'ca':'/ca','cert':'/cert'},
                        {'ca':'/ca','key':'/key'},{'ca':'/ca','verify':False},{'ca':'/bad\x00'}):
                with self.subTest(backend=backend,tls=tls),patch('atropos.sql_server.anysql.connect_by_uri') as connect,self.assertRaises(ValueError):
                    open_storage(dict(backend=backend,url=backend+'://user:secret@db.example/engine',options={'tls':tls}))
                connect.assert_not_called()
        with self.assertRaises(ValueError):
            server_uri(dict(backend='mysql',url='mysql://user:secret@localhost/engine',options={'tls':{'ca':'/ca'}}))

    def test_uri_cannot_weaken_or_override_tls_options(self):
        for backend in ('postgresql','mysql'):
            for query in ('sslmode=disable','ssl_mode=DISABLED','tls=disabled','tls_ca=/other',
                          'host=other','unix_socket=/socket','mode=ro','mode=rw&mode=rwc'):
                with self.subTest(backend=backend,query=query),self.assertRaises(ValueError):
                    server_uri(dict(backend=backend,url=backend+'://user:secret@db.example/engine?'+query,options={'tls':{'ca':'/ca'}}))
