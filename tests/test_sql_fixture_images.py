import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'sql-fixture-images.py'
spec = importlib.util.spec_from_file_location('sql_fixture_images', SCRIPT)
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


class SQLFixtureImageTests(unittest.TestCase):
    def fixture(self):
        lock = images.load_lock()
        source = b'#!/bin/bash\n# preserve TLS, ports, cleanup and deadlines\n'
        for backend in images.BACKENDS:
            tag = lock['images'][backend]['reference'].split('@')[0]
            source += f'{backend}) image={tag}; internal=1234 ;;\n'.encode()
        lock['tls_fixture']['sha256'] = hashlib.sha256(source).hexdigest()
        return lock, source

    def test_pin_changes_only_image_assignments(self):
        lock, source = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.sh'
            path.write_bytes(source)
            images.pin_fixture(path, lock)
            result = path.read_bytes()
            for entry in lock['images'].values():
                self.assertIn(entry['reference'].encode(), result)
                result = result.replace(entry['reference'].encode(),
                                        entry['reference'].split('@')[0].encode())
            self.assertEqual(result, source)
            with self.assertRaises(ValueError):
                images.pin_fixture(path, lock)

    def test_changed_or_ambiguous_fixture_is_not_written(self):
        lock, source = self.fixture()
        tag = lock['images']['mysql']['reference'].split('@')[0]
        variants = [(source + b'# drift', False),
                    (source + f'image={tag};'.encode(), True),
                    (source.replace(f'image={tag};'.encode(), b'image=unknown;'), True)]
        for content, update_hash in variants:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as root:
                candidate = copy.deepcopy(lock)
                if update_hash:
                    candidate['tls_fixture']['sha256'] = hashlib.sha256(content).hexdigest()
                path = Path(root) / 'fixture.sh'
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    images.pin_fixture(path, candidate)
                self.assertEqual(path.read_bytes(), content)

    def test_inspection_refuses_different_reference_image_or_platform(self):
        expected = images.load_lock()['images']['mysql']
        identity = {'reference': expected['reference'],
                    'image_id': expected['linux_amd64_image_id']}
        for field in ('reference', 'image_id'):
            changed = {**identity, field: 'unexpected'}
            with self.subTest(field=field), patch.object(images, 'command_output', return_value=json.dumps(changed)):
                with self.assertRaises(ValueError):
                    images.inspect_container('synthetic', expected)
        with patch.object(images, 'command_output', side_effect=[json.dumps(identity), 'linux/arm64']):
            with self.assertRaises(ValueError):
                images.inspect_container('synthetic', expected)
        with patch.object(images, 'command_output', side_effect=[json.dumps(identity), 'linux/amd64']):
            self.assertEqual(images.inspect_container('synthetic', expected), identity)

    def test_evidence_requires_every_tls_variant(self):
        lock = images.load_lock()
        with patch.object(images, 'inspect_container', return_value={}) as inspect:
            with patch.object(images, 'command_output', return_value='synthetic-source'):
                result = images.record('tls', lock)
        self.assertEqual(len(result['containers']), 9)
        self.assertEqual({call.args[0] for call in inspect.call_args_list},
                         {f'anysql-{kind}-{backend}' for backend in images.BACKENDS
                          for kind in ('tls', 'plain', 'expired')})
        with patch.object(images, 'inspect_container', side_effect=ValueError('missing fixture')):
            with self.assertRaises(ValueError):
                images.record('tls', lock)
