import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import redis


class RedisFixture:
    """Start a disposable server, never connect to an operator's Redis."""
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='atropos-test-')
        self.root = Path(self.directory.name)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.log = (self.root / 'redis.log').open('ab')
        self.binary = os.environ.get('ATROPOS_TEST_REDIS_SERVER') or shutil.which('redis-server')
        if not self.binary:
            try:
                import redislite
                self.binary = str(Path(redislite.__file__).parent / 'bin' / 'redis-server')
            except ImportError as exc:
                raise RuntimeError('Install Redis >=6.2 or set ATROPOS_TEST_REDIS_SERVER') from exc
        self.client = redis.Redis(host='127.0.0.1', port=self.port, decode_responses=True,
                                  socket_timeout=2, socket_connect_timeout=2)
        try:
            self.start()
        except Exception:
            self.close()
            raise

    def start(self):
        self.process = subprocess.Popen([
            self.binary, '--bind', '127.0.0.1', '--port', str(self.port),
            '--dir', str(self.root), '--save', '', '--appendonly', 'yes',
            '--appendfsync', 'always', '--maxmemory-policy', 'noeviction',
        ], stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                if self.client.ping():
                    return
            except redis.RedisError:
                time.sleep(.02)
        raise RuntimeError('Disposable Redis did not start: ' + (self.root / 'redis.log').read_text())

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.client.connection_pool.disconnect()

    def restart(self):
        self.stop()
        self.start()

    def close(self):
        self.stop()
        self.client.close()
        self.log.close()
        self.directory.cleanup()


def workflow(steps=None):
    return {'version': 1, 'name': 'test', 'start': 'first',
            'steps': steps or {'first': {'action': 'core.echo'}}}
