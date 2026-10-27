"""Exercise the installed subprocess transport without installing the engine."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (ACTOR, CLIENT_DISTRIBUTION, InstalledCase, LOOPBACK_HOST,
    LOOPBACK_ORIGIN, MAX_OUTPUT_BYTES, TOKEN, verify_package)
from atropos_client.client import RuleClient, RemoteRuleError
from atropos_client.protocol import ROUTE


class ClientPackageTests(InstalledCase):
    def test_installed_bytes_version_license_and_dependency_isolation(self):
        verify_package(self, 'atropos_client', CLIENT_DISTRIBUTION, 'client/atropos_client')
        for module in ('atropos', 'redis', 'zen', 'httpdis', 'dwho', 'jwt', 'auton'):
            self.assertIsNone(importlib.util.find_spec(module))

    def test_installed_worker_contacts_only_the_selected_loopback_fixture(self):
        rows, mode = [], ['ok']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_OUTPUT_BYTES:
                    self.send_error(400)
                    return
                rows.append(dict(path=self.path, authorized=self.headers.get('Authorization') == 'Bearer ' + TOKEN,
                    body=json.loads(self.rfile.read(length))))
                if mode[0] == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', '/credential-trap')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                body = json.dumps({'result': {'fixture': True}}).encode()
                self.send_response(401 if mode[0] == 'denied' else 200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Rule-Service', 'wrong' if mode[0] == 'wrong-service' else 'atropos')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer((LOOPBACK_HOST, 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.private('token', TOKEN)
        profile = self.private('client.json', json.dumps(dict(origin=LOOPBACK_ORIGIN.format(port=server.server_port),
            allow_loopback_http=True, token_file='token', timeout=3)))
        client = RuleClient(profile, expected_actor=ACTOR)
        try:
            self.assertEqual(client.catalog(), {'fixture': True})
            for selected in ('redirect', 'wrong-service'):
                mode[0] = selected
                with self.assertRaisesRegex(RemoteRuleError, '^rule_service_unavailable$'):
                    client.catalog()
            mode[0] = 'denied'
            with self.assertRaisesRegex(PermissionError, '^rule_access_denied$'):
                client.catalog()
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row == dict(path=ROUTE, authorized=True,
                body=dict(method='catalog', arguments={}, expected_actor=ACTOR)) for row in rows))
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
