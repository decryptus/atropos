"""Submit real jobs with both supported Atropos credential profiles."""
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

from atropos.actions import Context
from atropos.execution_adapters import configured_actions, validate_adapters

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ENGINE_DISTRIBUTION, InstalledCase, verify_package


class InstalledAutonTests(InstalledCase):
    def test_bearer_rotation_and_basic_dispatch(self):
        verify_package(self, 'atropos', ENGINE_DISTRIBUTION, 'atropos')
        binary = Path(os.environ['AUTON_TEST_DAEMON']).resolve(strict=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        effect = self.root / 'jobs-run'
        routes = {name: dict(handler='job_' + name, op=method,
                             regexp='^' + name + '/(?P<endpoint>[^/]+)/(?P<id>[a-z0-9-]{8,64})$')
                  for name, method in [('run', 'POST'), ('status', 'GET')]}
        routes['run']['safe_init'] = True
        conf = dict(general=dict(listen_addr='127.0.0.1', listen_port=port, max_requests=0,
                        max_life_time=0, max_workers=2, lock_timeout=60, charset='utf-8',
                        content_type='application/json', auth_mode='required',
                        authentication=dict(backend='sqlite', path=str(self.root / 'accounts.db'))),
                    modules={'job': {'routes': routes}}, endpoints={'probe': dict(plugin='subproc',
                        users={'operator': True}, config=dict(prog=str(binary.parent / 'python'),
                        args=['-c', f"with open({str(effect)!r}, 'a') as f: f.write('ran\\n')"],
                        **{'disallow-args': True, 'disallow-env': True, 'disallow-envfiles': True,
                           'disallow-argfiles': True}))})
        daemon_conf = self.private('auton.json', json.dumps(conf))
        admin = [str(binary.parent / 'autond-auth'), '-c', str(daemon_conf)]
        password = secrets.token_urlsafe(32)
        self.private('password', password)
        subprocess.run(admin + ['user', 'set', '-u', 'operator', '-s', 'read', '-s', 'run'],
                       input=(password + '\n') * 2, text=True, capture_output=True, check=True, timeout=10)
        def issue(filename):
            issued = subprocess.run(admin + ['token', 'create', '-u', 'operator', '-s', 'read', '-s', 'run',
                                    '-t', '600', '-o', str(self.root / filename)],
                                    capture_output=True, check=True, timeout=10)
            return json.loads(issued.stdout)['credential_id']
        original = issue('token')
        @contextmanager
        def running_daemon():
            daemon = subprocess.Popen([str(binary), '-f', '-c', str(daemon_conf), '-p', str(self.root/'pid'),
                                       '--logfile', str(self.root/'daemon.log')], cwd=self.root,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(150):
                    self.assertIsNone(daemon.poll(), 'daemon exited before readiness')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.05)
                yield
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)

        target = 'demo/server/lab'
        def adapter(credentials):
            spec = dict(backend='auton', target=target, endpoint='probe', allow_loopback_http=True,
                        origin=f'http://127.0.0.1:{port}', **credentials)
            adapters = {'ops.probe': spec}
            validate_adapters(adapters, [dict(organization='demo', kind='server', id='lab')])
            return configured_actions(adapters, self.root / 'engine.yml',
                                      lambda: ({'execution_adapters': adapters}, None))['ops.probe']
        def submit(action, run):
            result = asyncio.run(action.execute({}, Context(run, 'probe', target, 1, 10)))
            self.assertEqual((result['status'], result['return_code']), ('completed', 0))

        bearer = adapter({'token_file': 'token'})
        with running_daemon():
            submit(bearer, 'first-token')
            issue('replacement')
            subprocess.run(admin + ['token', 'revoke', '-i', original], capture_output=True, check=True, timeout=10)
            (self.root / 'replacement').replace(self.root / 'token')
            submit(bearer, 'rotated-token')
        del conf['general']['authentication']
        digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
        conf['general'].update(auth_basic='Legacy profile',
                               auth_basic_file=str(self.private('users', f'operator:{{SHA}}{digest}\n')))
        daemon_conf.write_text(json.dumps(conf))
        with running_daemon():
            submit(adapter({'username': 'operator', 'password_file': 'password'}), 'basic')
            self.assertEqual(effect.read_text().splitlines(), ['ran'] * 3)
