"""Real loopback receiver; no production notification endpoint is contacted."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from atropos.actions import ActionFailed, Context, OutcomeUnknown
from atropos.test_alert_delivery import configured_test_alert, validate_delivery
from atropos.worker import ExecutionDenied


class TestAlertDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = 'synthetic-test-receiver-token-'+'a'*32
        self.secret = self.root/'receiver.token'; self.secret.write_text(self.token); self.secret.chmod(0o600)
        self.received = []; self.mode = 'valid'
        owner = self
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.received.append((self.path, dict(self.headers), body))
                ack = {'version': 1, 'accepted': True, 'test_context': body['test_context'],
                       'delivery_id': body['delivery_id']}
                status = 202
                if owner.mode == 'redirect': status = 307
                if owner.mode == 'missing': del ack['test_context']
                if owner.mode == 'wrong-id': ack['test_context'] = {'test': True, 'id': 'b'*64}
                if owner.mode == 'numeric-flag': ack['test_context'] = dict(ack['test_context'], test=1)
                if owner.mode == 'wrong-delivery': ack['delivery_id'] = 'b'*64
                if owner.mode == 'refused': ack['accepted'] = False
                if owner.mode == 'delayed': time.sleep(1.3)
                raw = json.dumps(ack).encode()
                if owner.mode == 'oversized': raw = b' ' * 5000
                if owner.mode == 'duplicate': raw = raw[:-1] + b',"accepted":true}'
                try:
                    self.send_response(status); self.send_header('Content-Type', 'application/json')
                    self.send_header('Location', '/must-not-follow')
                    self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.config = {'backend': 'webhook', 'url': 'http://127.0.0.1:%d' % self.server.server_port,
                       'token_file': self.secret.name, 'allow_loopback_http': True, 'timeout': 2}
        self.document = {'test_alert_delivery': self.config}
        self.context = Context('a'*64, 'notify', 'container:web', 1, 5, True, 'a'*64)
        self.action = configured_test_alert(lambda: (self.document, None), self.root/'service.yml')

    def send(self):
        return asyncio.run(self.action.simulate({'subject': 'Intervention complete', 'url': 'https://ignored.invalid'}, self.context))

    def test_receiver_gets_same_context_authentication_and_stable_delivery_id(self):
        result = self.send(); path, headers, body = self.received[0]
        self.assertEqual(path, '/v1/test-alerts')
        self.assertEqual(headers['Authorization'], 'Bearer '+self.token)
        self.assertEqual(headers['X-Atropos-Test'], 'true')
        self.assertEqual(headers['X-Atropos-Test-ID'], self.context.test_id)
        self.assertEqual(headers['Idempotency-Key'], body['delivery_id'])
        self.assertEqual(body['test_context'], {'test': True, 'id': self.context.test_id})
        self.assertEqual(body['alert']['test_context'], body['test_context'])
        self.assertTrue(body['alert']['subject'].startswith('[TEST]'))
        self.assertEqual(result['alert']['delivery'], 'test_receiver_acknowledged')
        self.assertNotIn(self.token, json.dumps(result)); self.assertNotIn(self.config['url'], json.dumps(result))
        self.send(); self.assertEqual(self.received[1][2]['delivery_id'], body['delivery_id'])
        self.assertFalse(self.action.retry_safe)

    def test_invalid_or_unmarked_acknowledgements_are_uncertain_without_retry(self):
        for mode in ('missing', 'wrong-id', 'numeric-flag', 'wrong-delivery', 'refused', 'oversized', 'duplicate'):
            self.mode = mode; before = len(self.received)
            with self.subTest(mode=mode), self.assertRaises(OutcomeUnknown): self.send()
            self.assertEqual(len(self.received), before+1)

    def test_redirect_is_never_followed(self):
        self.mode = 'redirect'
        with self.assertRaises(OutcomeUnknown): self.send()
        self.assertEqual(len(self.received), 1)

    def test_timeout_is_bounded_and_does_not_retry(self):
        self.config['timeout'] = 1
        self.action = configured_test_alert(lambda: (self.document, None), self.root/'service.yml')
        self.mode = 'delayed'; start = time.monotonic()
        with self.assertRaises(OutcomeUnknown): self.send()
        self.assertLess(time.monotonic()-start, 2.5); self.assertEqual(len(self.received), 1)

    def test_real_execute_and_missing_context_never_send(self):
        with self.assertRaises(ActionFailed): asyncio.run(self.action.execute({}, self.context))
        for mode, test_id in ((False, 'a'*64), (True, None), (True, 'b'*64)):
            self.context = Context('a'*64, 'notify', 'target', 1, 5, mode, test_id)
            with self.assertRaises(ExecutionDenied): self.send()
        self.assertEqual(self.received, [])

    def test_changed_or_removed_destination_is_denied_before_send(self):
        self.document['test_alert_delivery'] = dict(self.config, url='https://other.invalid')
        with self.assertRaises(ExecutionDenied): self.send()
        self.document.clear()
        with self.assertRaises(ExecutionDenied): self.send()
        self.assertEqual(self.received, [])

    def test_adding_destination_does_not_promote_running_simulation(self):
        self.document.clear()
        self.action = configured_test_alert(lambda: (self.document, None), self.root/'service.yml')
        self.assertEqual(self.send()['alert']['delivery'], 'simulated')
        self.document['test_alert_delivery'] = self.config
        with self.assertRaises(ExecutionDenied): self.send()
        self.assertEqual(self.received, [])

    def test_private_token_is_reloaded_and_invalid_permissions_refuse_send(self):
        self.secret.chmod(0o644)
        with self.assertRaises(ExecutionDenied): self.send()
        self.secret.chmod(0o600); self.secret.write_text('b'*64)
        self.send()
        self.assertEqual(self.received[0][1]['Authorization'], 'Bearer '+'b'*64)

    def test_destination_validation_rejects_unsafe_or_ambiguous_configuration(self):
        for change in ({'backend':'smtp'}, {'url':'http://example.invalid'}, {'url':'http://localhost'},
                       {'url':'https://user:secret@example.invalid'}, {'url':'https://example.invalid/redirect'},
                       {'url':'https://example.invalid?token=x'}, {'url':'https://example.invalid#x'},
                       {'timeout':True}, {'timeout':31}, {'allow_loopback_http':1}, {'token_file':''}, {'extra':True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_delivery(dict(self.config, **change))
        secure = dict(self.config, url='https://receiver.example', allow_loopback_http=False)
        self.assertEqual(validate_delivery(secure)['url'], secure['url'])

    def test_https_verifies_certificates_and_does_not_use_environment_proxy(self):
        from atropos.test_alert_worker import deliver
        from atropos.test_alert_delivery import delivery_envelope
        from atropos.actions import simulate_alert
        config = dict(self.config, url='https://receiver.example', allow_loopback_http=False)
        envelope = delivery_envelope(asyncio.run(simulate_alert({},self.context))['alert'],self.context)
        with patch('atropos.test_alert_worker.http.client.HTTPSConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status=202; response.getheader.return_value='application/json'
            response.read.return_value=json.dumps({'version':1,'accepted':True,
                'test_context':envelope['test_context'],'delivery_id':envelope['delivery_id']}).encode()
            deliver({'configuration':config,'token':self.token,'envelope':envelope})
            context=factory.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            import ssl
            self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
            self.assertEqual(factory.call_args.args,('receiver.example',None))

    def test_transport_rejects_stripped_context_before_opening_connection(self):
        from atropos.test_alert_worker import deliver
        from atropos.test_alert_delivery import delivery_envelope
        from atropos.actions import simulate_alert
        envelope=delivery_envelope(asyncio.run(simulate_alert({},self.context))['alert'],self.context)
        del envelope['test_context']
        with patch('atropos.test_alert_worker.http.client.HTTPConnection') as connect:
            with self.assertRaises(ValueError):
                deliver({'configuration':self.config,'token':self.token,'envelope':envelope})
            connect.assert_not_called()

    def test_real_tls_accepts_trusted_certificate_and_refuses_untrusted_ca(self):
        import ssl
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
        self.config.update(url='https://127.0.0.1:'+str(server.server_port),allow_loopback_http=False,ca_file=cert.name)
        def restart():
            self.action=configured_test_alert(lambda:(self.document,None),self.root/'service.yml')
        restart()
        self.assertEqual(self.send()['alert']['delivery'],'test_receiver_acknowledged')
        self.assertEqual(self.received[0][2]['test_context'],{'test':True,'id':self.context.test_id})
        self.config['ca_file']=wrong.name
        with self.assertRaises(ExecutionDenied):self.send()
        restart()
        with self.assertRaises(OutcomeUnknown):self.send()
        self.assertEqual(len(self.received),1)
        self.config['ca_file']=cert.name;restart();cert.chmod(0o644)
        with self.assertRaises(ExecutionDenied):self.send()
        cert.chmod(0o600);cert.write_text('invalid CA')
        with self.assertRaises(ExecutionDenied):self.send()
        self.assertEqual(len(self.received),1)

    def test_invalid_ca_settings_are_rejected_offline(self):
        for value in ('',True,{},'x'*4097):
            with self.subTest(value=type(value)),self.assertRaises(ValueError):
                validate_delivery(dict(self.config,url='https://receiver.invalid',allow_loopback_http=False,ca_file=value))
        with self.assertRaises(ValueError):validate_delivery(dict(self.config,ca_file='ca.pem'))
