"""Exercise inbound HTTP read deadlines on the installed service handlers."""
from http.server import HTTPServer
import select
import socket
from threading import Thread
import unittest

from httpdis.httpdis import HttpResponse, HttpServerContext
from atropos.rule_server import ROUTE, RuleHandler


class ReadDeadlineTests(unittest.TestCase):
    def test_rule_command(self):
        self.check_deadlines(RuleHandler, ROUTE, 'application/json')

    def check_deadlines(self, handler, path, content_type, extra_headers=b''):
        context = HttpServerContext()
        requests = []

        def accept(request):
            requests.append(request.get_payload().getvalue())
            return HttpResponse(data='ok')

        context.register(accept, 'POST', name=path.lstrip('/'), max_body_size=256)
        context.init({})
        bound = context.bind_handler(handler)
        bound.header_read_seconds = 0.4
        bound.body_max_seconds = 0.4

        address = ('127.0.0.1', 0)
        with HTTPServer(address, bound) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                headers = (f'POST {path} HTTP/1.0\r\n'
                           f'Content-Type: {content_type}\r\n').encode() + extra_headers
                for phase, start in (
                        ('headers', headers + b'X-Slow: '),
                        ('body', headers + b'Content-Length: 128\r\n\r\n')):
                    with self.subTest(phase=phase), socket.create_connection(server.server_address, 3) as client:
                        client.sendall(start)
                        # Send data more often than the socket inactivity timeout.
                        for _ in range(40):
                            if select.select([client], [], [], 0.05)[0]:
                                break
                            client.sendall(b'x')
                        else:
                            self.fail(f'{phase} trickle exceeded its total read budget')
                        with client.makefile('rb') as reply:
                            status = reply.readline()
                        if phase == 'headers':
                            self.assertEqual(status, b'')
                        else:
                            self.assertEqual(status.split()[1:2], [b'408'])
                with socket.create_connection(server.server_address, 3) as client:
                    client.sendall(headers + b'Content-Length: 2\r\n\r\n{}')
                    with client.makefile('rb') as reply:
                        self.assertEqual(reply.readline().split()[1:2], [b'200'])
                self.assertEqual(requests, [b'{}'])
            finally:
                server.shutdown()
                worker.join()
