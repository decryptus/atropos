"""Role configuration and extensibility, without touching operator infrastructure."""
from contextlib import redirect_stdout, redirect_stderr
import io
import tempfile
import unittest
from unittest.mock import patch, Mock

from atropos.cli import main
from atropos.storage_config import backend_settings
from atropos.backend_registry import open_storage, open_queue


class StorageConfigurationTests(unittest.TestCase):
    def test_default_is_durable_storage_and_queue_roles(self):
        self.assertEqual(backend_settings({}),dict(storage={'backend':'sqlite','url':'sqlite:///atropos.db'},queue={'backend':'storage'}))
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ',{'ATROPOS_STORAGE_URL':'sqlite:///'+root+'/engine.db'},clear=True),redirect_stdout(io.StringIO()):
            self.assertEqual(main(['queue-status']),0)

    def test_arbitrary_installed_non_sql_adapters_receive_opaque_configuration(self):
        entry=Mock();entry.load.return_value=Mock(return_value='adapter')
        spec={'backend':'document-service','options':{'collection':'runs'}}
        with patch('atropos.backend_registry.entry_points',return_value=[entry]) as discover:
            self.assertEqual(open_storage(spec),'adapter')
            discover.assert_called_with(group='atropos.storage_backends',name='document-service')
            entry.load.return_value.assert_called_with(spec)
            self.assertEqual(open_queue(spec,'engine'),'adapter')
            discover.assert_called_with(group='atropos.queue_backends',name='document-service')
            entry.load.return_value.assert_called_with(spec,namespace='engine',lease_ms=30000)

    def test_unknown_ambiguous_and_legacy_configuration_fail_closed(self):
        cases=[{'ATROPOS_REDIS_URL':'redis://localhost'}, {'ATROPOS_STORAGE_URL':''},
               {'ATROPOS_STORAGE_BACKEND':'missing'}, {'ATROPOS_STORAGE_BACKEND':''},
               {'ATROPOS_STORAGE_OPTIONS':'[]'}, {'ATROPOS_STORAGE_OPTIONS':'broken'}]
        for env in cases:
            with self.subTest(env=env),patch.dict('os.environ',env,clear=True),redirect_stderr(io.StringIO()):
                self.assertEqual(main(['status','synthetic']),2)
        with patch('atropos.backend_registry.entry_points',return_value=[Mock(),Mock()]),self.assertRaises(ValueError):
            open_storage({'backend':'duplicate'})

    def test_configuration_errors_do_not_echo_credentials(self):
        output=io.StringIO()
        with patch.dict('os.environ',{'ATROPOS_STORAGE_BACKEND':'redis','ATROPOS_STORAGE_URL':'redis://user:SECRET_MARKER@host'},clear=True),redirect_stderr(output):
            self.assertEqual(main(['status','synthetic']),2)
        self.assertNotIn('SECRET_MARKER',output.getvalue())
