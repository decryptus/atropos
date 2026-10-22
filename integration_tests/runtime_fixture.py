"""Disposable infrastructure and a real response-dropping TCP/HTTP boundary."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import redis
import yaml


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def private(path, content):
    path.write_text(content);path.chmod(0o600)


def wait_for(probe, seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            result=probe()
            if result:return result
        except (OSError,redis.RedisError,http.client.HTTPException):pass
        time.sleep(.03)
    raise RuntimeError('Disposable service readiness deadline exceeded')


class Storage:
    def __init__(self, snapshot=None):
        self.directory=tempfile.TemporaryDirectory(prefix='atropos-recovery-')
        self.root=Path(self.directory.name);self.port=port();self.password=secrets.token_urlsafe(32)
        self.process=None;self.log=None
        try:
            if snapshot:
                manifest=json.loads((snapshot/'manifest.json').read_text())
                data=(snapshot/'dump.rdb').read_bytes()
                if manifest!={'version':1,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}:
                    raise ValueError('backup_integrity_failed')
                (self.root/'dump.rdb').write_bytes(data);(self.root/'dump.rdb').chmod(0o600)
            binary=os.environ.get('ATROPOS_TEST_REDIS_SERVER') or shutil.which('redis-server')
            if not binary:
                import redislite
                binary=str(Path(redislite.__file__).parent/'bin'/'redis-server')
            private(self.root/'redis.conf','bind 127.0.0.1\nport '+str(self.port)+'\ndir '+str(self.root)+
                '\nsave ""\nappendonly '+('no' if snapshot else 'yes')+'\nappendfsync always\nmaxmemory-policy noeviction\nrequirepass '+self.password+'\n')
            self.log=(self.root/'redis.log').open('ab')
            self.process=subprocess.Popen([binary,str(self.root/'redis.conf')],stdout=self.log,stderr=self.log)
            self.client=redis.Redis(host='127.0.0.1',port=self.port,password=self.password,decode_responses=True,
                socket_connect_timeout=1,socket_timeout=3)
            wait_for(lambda:self.process.poll() is None and self.client.ping())
            if snapshot:
                self.client.config_set('appendonly','yes')
                wait_for(lambda:not self.client.info('persistence')['aof_rewrite_in_progress'])
                if self.client.info('persistence')['aof_last_bgrewrite_status']!='ok':
                    raise RuntimeError('restored_aof_failed')
                self.client.config_rewrite()
        except Exception:
            self.close();raise

    def snapshot(self, destination):
        # Call only after writers/workers have been quiesced. This is a fixture,
        # not an online backup API or an operator-facing arbitrary-path restore.
        destination.mkdir(mode=0o700)
        self.client.save()
        data=(self.root/'dump.rdb').read_bytes()
        (destination/'dump.rdb').write_bytes(data);(destination/'dump.rdb').chmod(0o600)
        private(destination/'manifest.json',json.dumps(dict(version=1,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))))
        return destination

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate();self.process.wait(timeout=10)
        if hasattr(self,'client'):self.client.close()
        if self.log:self.log.close()
        self.directory.cleanup()


class AutonDaemon:
    def __init__(self):
        source=os.environ.get('AUTON_TEST_SOURCE');binary=os.environ.get('AUTON_TEST_DAEMON')
        if not source or not binary or not Path(binary).is_absolute():
            raise RuntimeError('Set AUTON_TEST_SOURCE and AUTON_TEST_DAEMON to the pinned source and installed daemon')
        self.directory=tempfile.TemporaryDirectory(prefix='atropos-auton-');self.root=Path(self.directory.name)
        self.process=None;self.log=None;self.port=port();self.origin='http://127.0.0.1:'+str(self.port)
        self.password=secrets.token_urlsafe(32);self.auth=('operator',self.password)
        try:
            digest=base64.b64encode(hashlib.sha1(self.password.encode()).digest()).decode()
            private(self.root/'users','operator:{SHA}'+digest+'\n')
            config=yaml.safe_load((Path(source)/'etc/auton/auton.yml.example').read_text())
            config['general'].update(listen_addr='127.0.0.1',listen_port=self.port,max_life_time=0,max_requests=0,
                auth_basic='Integration',auth_basic_file=str(self.root/'users'),
                job_storage=dict(backend='sqlite',path=str(self.root/'jobs.db'),timeout=3))
            config.pop('import_modules',None)
            config['modules']=yaml.safe_load((Path(source)/'etc/auton/modules/job.yml').read_text())
            config['endpoints']={'approved':dict(plugin='subproc',config=dict(prog=str(Path(binary).parent/'python'),timeout=10))}
            private(self.root/'config.yml',yaml.safe_dump(config))
            self.log=(self.root/'process.log').open('ab')
            env={key:os.environ[key] for key in ('PATH','LANG') if key in os.environ}
            env['PYTHONNOUSERSITE']='1'
            self.process=subprocess.Popen([binary,'-f','-c',str(self.root/'config.yml'),'-p',str(self.root/'daemon.pid'),
                '--logfile',str(self.root/'daemon.log')],cwd=self.root,env=env,stdout=self.log,stderr=self.log)
            wait_for(lambda:self.process.poll() is None and self.request('GET','/health')[0]==200)
        except Exception:
            self.close();raise

    def request(self, method, path, body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        try:
            headers={'Authorization':'Basic '+base64.b64encode(('operator:'+self.password).encode()).decode()}
            conn.request(method,path,body=body,headers=headers)
            response=conn.getresponse();return response.status,json.loads(response.read())
        finally:conn.close()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate();self.process.wait(timeout=10)
        if self.log:self.log.close()
        self.directory.cleanup()


class DropResponseProxy:
    """Forward a real admitted POST, then lose its response and all observations."""
    def __init__(self, daemon):
        self.submissions=0;self.admitted_status=None
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                owner.submissions+=1
                conn=http.client.HTTPConnection('127.0.0.1',daemon.port,timeout=3)
                try:
                    raw=self.rfile.read(int(self.headers['Content-Length']))
                    conn.request('POST',self.path,body=raw,headers={key:value for key,value in self.headers.items()
                        if key.lower() in ('authorization','content-type','content-length')})
                    response=conn.getresponse();owner.admitted_status=response.status;response.read()
                finally:conn.close()
                self.close_connection=True
            def do_GET(self):
                if self.path != '/health':
                    self.close_connection=True
                    return
                conn=http.client.HTTPConnection('127.0.0.1',daemon.port,timeout=3)
                try:
                    conn.request('GET',self.path,headers={'Authorization':self.headers.get('Authorization','')})
                    response=conn.getresponse();body=response.read()
                    self.send_response(response.status)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)))
                    self.end_headers();self.wfile.write(body)
                finally:conn.close()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)


class AlertReceiver:
    """Contract fixture, optionally forwarding to the disposable CI Alertmanager."""
    def __init__(self):
        self.token=secrets.token_urlsafe(32);self.requests=[];self.mode='ok'
        configured=os.environ.get('ATROPOS_TEST_ALERTMANAGER_PORT')
        self.alertmanager_port=int(configured) if configured else None
        if self.alertmanager_port is not None and not 1<=self.alertmanager_port<=65535:
            raise ValueError('invalid_disposable_alertmanager_port')
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+owner.token:
                    self.send_error(401);return
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path,body))
                if self.path=='/api/v2/alerts' and owner.alertmanager_port is not None:
                    connection=http.client.HTTPConnection('127.0.0.1',owner.alertmanager_port,timeout=3)
                    try:
                        connection.request('POST','/api/v2/alerts',json.dumps(body),{'Content-Type':'application/json'})
                        response=connection.getresponse();response.read()
                        if response.status!=200:self.send_error(502);return
                        connection.request('GET','/api/v2/alerts')
                        response=connection.getresponse();stored=json.loads(response.read())
                        identity=body[0]['labels']['atropos_delivery_id']
                        if response.status!=200 or not any(a['labels'].get('atropos_delivery_id')==identity for a in stored):
                            self.send_error(502);return
                    finally:connection.close()
                if owner.mode=='lost':self.close_connection=True;return
                response=(dict(version=1,accepted=True,test_context=body['test_context'],delivery_id=body['delivery_id'])
                          if self.path=='/v1/test-alerts' else None)
                raw=json.dumps(response).encode() if response else b''
                self.send_response(202 if response else 200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
