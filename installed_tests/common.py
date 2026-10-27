"""Acceptance helpers; read candidate bytes without importing its packages."""
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI_TIMEOUT = 20
MAX_OUTPUT_BYTES = 65536
ENGINE_DISTRIBUTION = 'atraxis-atropos'
CLIENT_DISTRIBUTION = 'atropos-client'
NAMESPACE = 'installed'
TOKEN = 'a' * 43
ACTOR = dict(subject='reader', organization='lab')
LOOPBACK_HOST = '127.0.0.1'
LOOPBACK_ORIGIN = 'http://' + LOOPBACK_HOST + ':{port}'
WORKFLOW = dict(version=1, name='installed-echo', start='echo',
    steps=dict(echo=dict(action='core.echo', **{'with': {'message': 'installed-wheel'}})))


def verify_package(case, module_name, distribution_name, source):
    module = importlib.import_module(module_name)
    package = Path(module.__file__).resolve().parent
    case.assertTrue(package.is_relative_to(Path(sysconfig.get_path('purelib')).resolve()),
        'Package must be imported from the fresh installation')
    distribution = importlib.metadata.distribution(distribution_name)
    case.assertEqual(module.__version__, distribution.version)
    def files(directory):
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*.py')}
    case.assertEqual(files(package), files(ROOT / source))
    licenses = [p for p in distribution.files if str(p).endswith('.dist-info/licenses/LICENSE')
        or str(p).endswith('.dist-info/LICENSE')]
    case.assertEqual(len(licenses), 1)
    case.assertEqual(distribution.locate_file(licenses[0]).read_bytes(), (ROOT / 'LICENSE').read_bytes())
    for name in ('atraxis',):
        case.assertIsNone(importlib.util.find_spec(name))


class InstalledCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='atropos-installed-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        self.environment.update(PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1')
        self.command = [str(Path(sys.executable).parent / 'atropos'), '--namespace', NAMESPACE]

    def private(self, name, value):
        path = self.root / name
        path.write_text(value)
        path.chmod(0o600)
        return path

    def cli(self, *args, database='engine.db', code=0):
        environment = dict(self.environment, ATROPOS_STORAGE_BACKEND='sqlite',
            ATROPOS_STORAGE_URL='sqlite:///' + str(self.root / database), ATROPOS_QUEUE_BACKEND='storage')
        result = subprocess.run(self.command + list(map(str, args)), cwd=self.root,
            env=environment, capture_output=True, text=True, timeout=CLI_TIMEOUT)
        self.assertLessEqual(len(result.stdout.encode()) + len(result.stderr.encode()), MAX_OUTPUT_BYTES)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def value(self, *args, **kwargs):
        return json.loads(self.cli(*args, **kwargs).stdout)

    def workflow(self):
        return self.private('workflow.json', json.dumps(WORKFLOW))

    def profile(self, database):
        return self.private(database + '.json', json.dumps(dict(version=1, namespace=NAMESPACE,
            storage=dict(backend='sqlite', url='sqlite:///' + str(self.root / database)),
            identities=[dict(ACTOR, token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest())],
            resources=[], grants=[])))
