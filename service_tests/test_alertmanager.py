"""Real disposable HTTP boundary; no external notification service."""
import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from atropos.actions import Context, OutcomeUnknown, builtin_actions
from atropos.alertmanager_adapter import configured_alertmanager
from atropos.dispatch import ApprovedBindings
from atropos.execution_adapters import adapter_revision, configured_actions, validate_adapters
from atropos.worker import ExecutionDenied


class AlertmanagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = 'synthetic-' + 'a'*48
        secret = self.root/'token'; secret.write_text(self.token); secret.chmod(0o600)
        self.requests = []; self.mode = 'ok'
        owner = self
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, dict(self.headers), body))
                if owner.mode == 'lost': self.close_connection = True; return
                if owner.mode == 'slow': time.sleep(1.5)
                status = {'redirect':307, 'refused':401, 'server':500}.get(owner.mode, 200)
                if self.path == '/v1/test-alerts':
                    response = json.dumps(dict(version=1, accepted=True,
                        test_context=body['test_context'], delivery_id=body['delivery_id'])).encode()
                else: response = b'x'*5000 if owner.mode == 'oversized' else b''
                try:
                    self.send_response(status)
                    self.send_header('Location', '/must-not-follow')
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(response)))
                    self.end_headers(); self.wfile.write(response)
                except OSError: pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.spec = dict(backend='alertmanager', target='demo/container/web',
            origin='http://127.0.0.1:'+str(self.server.server_port), allow_loopback_http=True,
            token_file='token', labels={'alertname':'InterventionComplete','severity':'info'}, simulation='test_receiver')
        self.document = dict(execution_adapters={'ops.notify':self.spec})
        self.loader = lambda: (deepcopy(self.document), None)
        self.context = Context('a'*64, 'notify', 'demo/container/web', 1, 5)
        self.resources = [dict(organization='demo', kind='container', id='web')]

    def action(self):
        return configured_alertmanager('ops.notify', self.spec, self.root/'engine.yml', self.loader)

    def send(self):
        return asyncio.run(self.action().execute({'subject':'Completed'}, self.context))

    def test_opt_in_post_has_fixed_route_labels_and_bounded_lifetime(self):
        result = self.send(); path, headers, body = self.requests[0]
        self.assertEqual(path, '/api/v2/alerts')
        self.assertEqual(headers['Authorization'], 'Bearer '+self.token)
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['labels']['alertname'], 'InterventionComplete')
        self.assertEqual(body[0]['labels']['atropos_target'], self.context.target)
        self.assertEqual(body[0]['annotations']['atropos_run_id'], self.context.run_id)
        from datetime import datetime
        self.assertEqual((datetime.fromisoformat(body[0]['endsAt'])-datetime.fromisoformat(body[0]['startsAt'])).total_seconds(),300)
        self.assertEqual(result['alert']['delivery'], 'alertmanager_accepted')
        self.assertEqual(body[0]['labels']['atropos_delivery_id'], result['alert']['delivery_id'])
        self.assertNotIn(self.token,json.dumps(result)); self.assertNotIn(self.spec['origin'],json.dumps(result))
        self.assertFalse(self.action().retry_safe)
        self.assertEqual(builtin_actions()['core.alert'].configuration_revision,None)

    def test_unknown_reply_never_retries_or_follows_redirect(self):
        for mode in ('lost','redirect','refused','server','oversized'):
            self.mode = mode; before = len(self.requests)
            with self.subTest(mode=mode), self.assertRaises(OutcomeUnknown): self.send()
            self.assertEqual(len(self.requests),before+1)

    def test_deadline_terminates_transport(self):
        self.mode = 'slow'; self.spec['timeout'] = 1
        start = time.monotonic()
        with self.assertRaises(OutcomeUnknown): self.send()
        self.assertLess(time.monotonic()-start,2.5)
        self.assertEqual(len(self.requests),1)

    def test_target_test_marker_inputs_and_private_secret_fail_before_network(self):
        for context in (Context('a'*64,'notify','other',1,5),
                        Context('a'*64,'notify',self.spec['target'],1,5,True,'a'*64),
                        Context('a'*64,'notify',self.spec['target'],1,5,False,'a'*64)):
            with self.subTest(context=context), self.assertRaises(ExecutionDenied):
                asyncio.run(self.action().execute({},context))
        for params in ({'url':'http://other.invalid'}, {'labels':{'severity':'critical'}}, {'subject':True}, {'description':'x'*4097}):
            with self.subTest(params=params), self.assertRaises(ExecutionDenied):
                asyncio.run(self.action().execute(params,self.context))
        (self.root/'token').chmod(0o644)
        with self.assertRaises(ExecutionDenied): self.send()
        self.assertEqual(self.requests,[])

    def test_destination_change_and_missing_binding_revision_are_refused(self):
        actions = configured_actions(self.document['execution_adapters'],self.root/'engine.yml',self.loader)
        binding = dict(organization='demo',rule='cpu',revision='a'*32,result=dict(decision='yes',matched_rule='high'),
            input={},max_age_seconds=60,workflow=dict(version=1,name='notify',start='alert',steps={'alert':dict(action='ops.notify')}))
        with self.assertRaisesRegex(ValueError,'revision_mismatch'): ApprovedBindings([binding],actions)
        binding['adapter_revisions'] = {'ops.notify':adapter_revision(self.spec)}
        ApprovedBindings([binding],actions)
        action = self.action(); self.spec['labels']['severity'] = 'warning'
        with self.assertRaises(ExecutionDenied): asyncio.run(action.execute({},self.context))
        self.assertEqual(self.requests,[])

    def test_simulation_never_uses_live_secret_or_destination(self):
        context = Context('b'*64,'notify',self.spec['target'],1,5,True,'b'*64)
        (self.root/'token').unlink()
        result = asyncio.run(self.action().simulate({'subject':'Simulation'},context))
        self.assertEqual(result['alert']['delivery'],'simulated'); self.assertEqual(self.requests,[])
        self.assertEqual(result['alert']['test_context'],dict(test=True,id='b'*64))
        self.spec['simulation'] = 'unsupported'
        self.assertIsNone(self.action().simulate)

    def test_explicit_test_receiver_preserves_marker_without_live_credentials(self):
        self.document['test_alert_delivery'] = dict(backend='webhook',url=self.spec['origin'],token_file='test.token',allow_loopback_http=True)
        test_token = self.root/'test.token'; test_token.write_text('b'*64); test_token.chmod(0o600)
        (self.root/'token').unlink()
        context = Context('b'*64,'notify',self.spec['target'],1,5,True,'b'*64)
        result = asyncio.run(self.action().simulate({'subject':'Simulation'},context))
        self.assertEqual(result['alert']['delivery'],'test_receiver_acknowledged')
        self.assertEqual(self.requests[0][0],'/v1/test-alerts')
        self.assertEqual(self.requests[0][2]['test_context'],dict(test=True,id=context.test_id))
        self.assertTrue(self.requests[0][2]['alert']['subject'].startswith('[TEST]'))

    def test_configuration_rejects_unsafe_origins_labels_limits_and_unknown_fields(self):
        validate_adapters({'ops.notify':self.spec},self.resources)
        for values in (dict(origin='http://remote.example'),dict(origin='http://localhost'),
                       dict(origin='https://remote.example/path',allow_loopback_http=False),
                       dict(target='other'),dict(timeout=True),dict(lifetime_seconds=3601),
                       dict(verify=False),dict(labels={'alertname':'x','atropos_target':'other'}),
                       dict(labels={'alertname':'x','__name__':'other'}),dict(labels={}),dict(simulation='noop')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_adapters({'ops.notify':dict(self.spec,**values)},self.resources)

    def test_https_checks_host_and_ca_with_no_environment_proxy(self):
        from atropos.alertmanager_worker import deliver
        spec = dict(self.spec,origin='https://receiver.example',allow_loopback_http=False)
        with patch('atropos.alertmanager_worker.http.client.HTTPSConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200; response.read.return_value = b''
            deliver(dict(configuration=spec,token=self.token,ca=None,payload=[]))
            context = factory.call_args.kwargs['context']
            self.assertTrue(context.check_hostname); self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
            self.assertEqual(factory.call_args.args,('receiver.example',None))

    def test_real_tls_accepts_trusted_certificate_and_refuses_untrusted_ca(self):
        from datetime import datetime, timedelta, timezone
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        def certificate(prefix):
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Disposable test')])
            now=datetime.now(timezone.utc)
            cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                .not_valid_after(now+timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
                .sign(key,hashes.SHA256()))
            cert_path=self.root/(prefix+'.pem');cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM));cert_path.chmod(0o600)
            key_path=self.root/(prefix+'.key');key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));key_path.chmod(0o600)
            return cert_path,key_path
        cert,key=certificate('trusted');wrong,_=certificate('untrusted')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        server=ThreadingHTTPServer(('127.0.0.1',0),self.server.RequestHandlerClass)
        server.socket=context.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        self.spec.update(origin='https://127.0.0.1:'+str(server.server_port),allow_loopback_http=False,ca_file=cert.name)
        self.assertEqual(self.send()['alert']['delivery'],'alertmanager_accepted')
        self.spec['ca_file']=wrong.name
        with self.assertRaises(OutcomeUnknown):self.send()
        self.assertEqual(len(self.requests),1)
