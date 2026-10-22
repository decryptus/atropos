"""Installed service/worker/backup commands with actual Auton and fresh storage."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

import yaml
from atropos_client.client import RuleClient
from atropos.authorization import OPERATIONS
from runtime_fixture import AlertReceiver, AutonDaemon, DropResponseProxy, Storage, port, private, wait_for


class ConfiguredOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='atropos-operator-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.daemon = AutonDaemon(); self.addCleanup(self.daemon.close)
        self.storage = Storage(); self.addCleanup(self.storage.close)
        self.api_port = port(); self.process = None; self.log = None; self.addCleanup(self.stop_service)
        self.token = secrets.token_urlsafe(32)
        self.env = {key: os.environ[key] for key in ('PATH','LANG') if key in os.environ}
        self.env['PYTHONNOUSERSITE'] = '1'
        self.spec = dict(backend='auton', target='demo/server/lab', origin=self.daemon.origin,
                        endpoint='approved', username='operator', password_file='executor.password',
                        allow_loopback_http=True, simulation='noop')
        self.config = dict(version=1, namespace='acceptance', storage=self.destination(self.storage),
            identities=[dict(token_sha256=hashlib.sha256(self.token.encode()).hexdigest(),subject='operator',organization='demo')],
            resources=[dict(organization='demo',kind='server',id='lab')],
            grants=[dict(subject='operator',organization='demo',rule_kind='intervention',operations=sorted(OPERATIONS),
                         scopes=[['server','lab']],actions=['ops.approved'],providers=['internal'])],
            execution_adapters={'ops.approved':self.spec},dispatch_bindings='bindings.yml')
        private(self.root/'executor.password', self.daemon.password)
        private(self.root/'token', self.token)
        self.yaml('client.yml',dict(origin='http://127.0.0.1:'+str(self.api_port),allow_loopback_http=True,
                                    token_file='token',timeout=5))
        self.yaml('bindings.yml',[]); self.write_config(); self.start_service()
        self.client = RuleClient(self.root/'client.yml',expected_actor={'subject':'operator','organization':'demo'})
        self.addCleanup(self.client.close)
        self.first = self.client.create_from_templates(['cpu-high'],name='cpu',rule_kind='intervention',selections=[['server','lab']])
        self.facts = {'cpu_percent':95}; self.result = {'decision':'review','matched_rule':'combined'}
        self.report = self.client.test('cpu',self.first['revision'],[{'name':'high','input':self.facts,'expected':self.result}])
        self.active = self.client.activate('cpu',self.first['revision'],test_id=self.report['id'],expected_generation=None)
        code = "from pathlib import Path; p=Path(%r); p.write_text(p.read_text()+'effect\\n' if p.exists() else 'effect\\n')" % str(self.daemon.root/'effect.txt')
        self.binding = dict(organization='demo',rule='cpu',revision=self.first['revision'],result=self.result,
            input={},max_age_seconds=300,workflow={'version':1,'name':'approved','start':'run',
                'steps':{'run':{'action':'ops.approved','timeout':5,'with':{'args':['-c',code]}}}})
        self.write_binding()

    @staticmethod
    def destination(storage):
        return dict(backend='redis',url='redis://:'+storage.password+'@127.0.0.1:'+str(storage.port)+'/0')

    def yaml(self,name,value):
        private(self.root/name,yaml.safe_dump(json.loads(json.dumps(value))))

    def write_config(self): self.yaml('engine.yml',self.config)

    def cli(self,*args,expected=0):
        result = subprocess.run([sys.executable,'-m','atropos.cli',*args],cwd=self.root,env=self.env,
                                capture_output=True,text=True,timeout=15)
        for secret in (self.token,self.daemon.password,self.storage.password):
            self.assertNotIn(secret,result.stdout+result.stderr)
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def write_binding(self):
        adapter = self.cli('execution-adapters','--config',str(self.root/'engine.yml'))
        self.binding['adapter_revisions'] = {step['action']:adapter[step['action']]['revision']
            for step in self.binding['workflow']['steps'].values() if step['action'] in adapter}
        self.yaml('bindings.yml',[self.binding])
        listing = self.cli('rules-bindings','--config',str(self.root/'engine.yml'))
        self.binding_id = listing[0]['id']

    def start_service(self):
        self.log=(self.root/'service.log').open('ab')
        self.process=subprocess.Popen([sys.executable,'-m','atropos.cli','rules-serve','--config',
            str(self.root/'engine.yml'),'--port',str(self.api_port)],cwd=self.root,env=self.env,
            stdout=self.log,stderr=self.log)
        probe = RuleClient(self.root/'client.yml',expected_actor={'subject':'operator','organization':'demo'})
        try:
            def ready():
                if self.process.poll() is not None:raise AssertionError('Installed service failed at startup')
                try:return bool(probe.catalog())
                except (OSError,ValueError,RuntimeError):return False
            wait_for(ready)
        finally:probe.close()

    def stop_service(self):
        if self.process is not None:
            if self.process.poll() is None:self.process.terminate();self.process.wait(timeout=10)
            self.assertEqual(self.process.returncode,0)
            self.process=None
        if self.log:self.log.close();self.log=None

    def submit(self,event='event'):
        return self.client.dispatch('cpu',['server','lab'],self.facts,self.binding_id,
                                    expected_generation=self.active['generation'],event_id=event)

    def worker(self,*,test=False,expected=0):
        return self.cli('rules-worker','--config',str(self.root/'engine.yml'),'--once',
                        *(['--test'] if test else []),expected=expected)

    def effects(self):
        path=self.daemon.root/'effect.txt';return path.read_text().splitlines() if path.exists() else []

    def test_installed_service_worker_and_client_execute_reviewed_adapter(self):
        receipt=self.submit();self.worker()
        state=self.client.dispatch_status('cpu','event')
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual(self.effects(),['effect'])
        self.assertEqual(self.submit()['id'],receipt['id'])
        self.worker();self.assertEqual(self.effects(),['effect'])

    def test_marked_noop_simulator_runs_without_credentials_or_remote_effect(self):
        (self.root/'executor.password').unlink()
        bindings=self.client.test_bindings('cpu',['server','lab'])
        self.assertTrue(bindings[0]['test_supported'])
        receipt=self.client.test_dispatch('cpu',['server','lab'],self.facts,self.binding_id,event_id='test')
        self.worker(test=True)
        state=self.client.test_dispatch_status('cpu','test')
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual(state['test_context'],receipt['test_context'])
        self.assertEqual(state['results']['run']['test_context'],receipt['test_context'])
        self.assertEqual(state['results']['run']['_test_context'],receipt['test_context'])
        self.assertEqual(state['results']['run']['effects'],'none')
        self.assertEqual(self.effects(),[])
        self.assertEqual([entry['operation'] for entry in self.client.audit('cpu')],['activate','test_run'])

    def test_destination_change_cannot_retarget_an_already_admitted_run(self):
        self.submit()
        self.spec['endpoint']='another-approved-endpoint';self.write_config()
        self.worker(expected=2)  # Old binding does not approve the new adapter revision.
        self.assertEqual(self.effects(),[])
        self.assertEqual(self.client.dispatch_status('cpu','event')['status'],'queued')
        self.write_binding()  # New explicit approval has a different binding ID.
        self.worker()
        self.assertEqual(self.client.dispatch_status('cpu','event')['status'],'cancelled')
        self.assertEqual(self.effects(),[])

    def test_operator_backup_restores_uncertainty_and_requires_explicit_resume(self):
        proxy=DropResponseProxy(self.daemon);self.addCleanup(proxy.close)
        self.spec['origin']=proxy.origin;self.write_config();self.write_binding()
        receipt=self.submit();self.worker();wait_for(lambda:len(self.effects())==1)
        original=self.client.dispatch_status('cpu','event')
        self.assertEqual(original['status'],'uncertain');self.assertEqual(proxy.submissions,1)
        audit=self.client.audit('cpu')
        self.cli('storage-pause','--config',str(self.root/'engine.yml'))
        archive=self.root/'storage.json'
        saved=self.cli('storage-backup','--config',str(self.root/'engine.yml'),'--output',str(archive))
        self.assertEqual(self.cli('storage-backup-inspect','--input',str(archive)),saved)
        self.stop_service();self.storage.close()
        replacement=Storage();self.addCleanup(replacement.close)
        self.config['storage']=self.destination(replacement);self.write_config()
        self.cli('storage-restore','--config',str(self.root/'engine.yml'),'--input',str(archive),expected=2)
        restored=self.cli('storage-restore','--config',str(self.root/'engine.yml'),'--input',str(archive),'--confirm-source-stopped')
        self.assertEqual(restored['execution'],'paused')
        self.start_service()
        self.assertEqual(self.client.active('cpu'),self.active)
        self.assertEqual(self.client.test_report('cpu',self.first['revision'],self.report['id']),self.report)
        self.assertEqual(self.client.audit('cpu'),audit)
        self.assertEqual(self.client.dispatch_status('cpu','event'),original)
        self.assertEqual(self.submit()['id'],receipt['id'])
        self.worker();self.assertEqual(proxy.submissions,1);self.assertEqual(self.effects(),['effect'])
        self.cli('storage-resume','--config',str(self.root/'engine.yml'),expected=2)
        self.assertEqual(replacement.client.get('acceptance:execution-paused'),'1')
        self.cli('storage-resume','--config',str(self.root/'engine.yml'),'--confirm-reconciled')
        # Resume permits execution; it never clears unresolved target locks.
        target='acceptance:target:'+hashlib.sha256(b'demo/server/lab').hexdigest()
        self.assertEqual(replacement.client.get(target),receipt['id'])
        self.worker();self.assertEqual(self.effects(),['effect'])

    def test_paused_real_and_test_admission_restore_then_guarded_execution(self):
        self.cli('storage-pause','--config',str(self.root/'engine.yml'))
        receipt=self.submit('paused')
        test=self.client.test_dispatch('cpu',['server','lab'],self.facts,self.binding_id,event_id='paused')
        self.worker();self.worker(test=True)
        self.assertEqual(self.effects(),[])
        self.assertEqual(self.client.dispatch_status('cpu','paused')['status'],'queued')
        self.assertEqual(self.client.test_dispatch_status('cpu','paused')['status'],'queued')
        archive=self.root/'queued.json'
        self.cli('storage-backup','--config',str(self.root/'engine.yml'),'--output',str(archive))
        self.stop_service();self.storage.close()
        replacement=Storage();self.addCleanup(replacement.close)
        self.config['storage']=self.destination(replacement);self.write_config()
        self.cli('storage-restore','--config',str(self.root/'engine.yml'),'--input',str(archive),'--confirm-source-stopped')
        self.start_service()
        self.assertEqual(self.submit('paused')['id'],receipt['id'])
        self.worker();self.assertEqual(self.effects(),[])
        self.cli('storage-resume','--config',str(self.root/'engine.yml'),'--confirm-reconciled')
        self.worker(test=True)
        result=self.client.test_dispatch_status('cpu','paused')
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['test_context'],test['test_context'])
        self.assertEqual(self.effects(),[])
        self.worker();self.assertEqual(self.effects(),['effect'])
        self.assertEqual(self.client.dispatch_status('cpu','paused')['status'],'succeeded')

    def test_paused_admission_is_cancelled_after_permission_revocation(self):
        self.cli('storage-pause','--config',str(self.root/'engine.yml'))
        self.submit('revoke')
        self.config['grants'][0]['actions']=[];self.write_config()
        self.cli('storage-resume','--config',str(self.root/'engine.yml'),'--confirm-reconciled')
        self.worker()
        self.assertEqual(self.client.dispatch_status('cpu','revoke')['status'],'cancelled')
        self.assertEqual(self.effects(),[])

    def configure_alert(self):
        self.receiver=AlertReceiver();self.addCleanup(self.receiver.close)
        private(self.root/'alert.token',self.receiver.token)
        self.config['execution_adapters']['ops.notify']=dict(backend='alertmanager',target='demo/server/lab',
            origin=self.receiver.origin,token_file='alert.token',allow_loopback_http=True,
            labels={'alertname':'InterventionComplete'},simulation='test_receiver')
        self.config['test_alert_delivery']=dict(backend='webhook',url=self.receiver.origin,
            token_file='alert.token',allow_loopback_http=True)
        self.config['grants'][0]['actions'].append('ops.notify')
        self.binding['workflow']['steps']['run']['next']='notify'
        self.binding['workflow']['steps']['notify']={'action':'ops.notify','timeout':5,
            'with':{'subject':'Intervention completed'}}
        self.write_config();self.write_binding()
        self.stop_service();self.start_service()  # Explicitly review and reload the enlarged registry.

    def test_auton_completion_precedes_alert_and_duplicate_never_replays(self):
        self.configure_alert();receipt=self.submit('alert')
        self.worker();self.assertEqual(self.effects(),['effect']);self.assertEqual(self.receiver.requests,[])
        self.worker();state=self.client.dispatch_status('cpu','alert')
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual([item['step'] for item in state['history'] if item['status']!='running'],['run','notify'])
        self.assertEqual(self.receiver.requests[0][0],'/api/v2/alerts')
        self.assertEqual(self.receiver.requests[0][1][0]['annotations']['atropos_run_id'],receipt['id'])
        self.assertEqual(self.submit('alert')['id'],receipt['id'])
        self.worker();self.assertEqual(len(self.receiver.requests),1);self.assertEqual(self.effects(),['effect'])

    def test_lost_alert_reply_preserves_uncertainty_lock_and_completed_auton_step(self):
        self.configure_alert();self.receiver.mode='lost';receipt=self.submit('lost-alert')
        self.worker();self.worker()
        self.assertEqual(self.client.dispatch_status('cpu','lost-alert')['status'],'uncertain')
        lock='acceptance:target:'+hashlib.sha256(b'demo/server/lab').hexdigest()
        self.assertEqual(self.storage.client.get(lock),receipt['id'])
        self.worker();self.assertEqual(len(self.receiver.requests),1);self.assertEqual(self.effects(),['effect'])

    def test_alert_permission_revocation_between_steps_prevents_notification(self):
        self.configure_alert();self.submit('revoke-alert');self.worker()
        self.config['grants'][0]['actions'].remove('ops.notify');self.write_config();self.worker()
        self.assertEqual(self.client.dispatch_status('cpu','revoke-alert')['status'],'cancelled')
        self.assertEqual(self.effects(),['effect']);self.assertEqual(self.receiver.requests,[])

    def test_auton_simulator_and_test_receiver_keep_one_marker_and_no_remote_job(self):
        self.configure_alert();(self.root/'executor.password').unlink()
        receipt=self.client.test_dispatch('cpu',['server','lab'],self.facts,self.binding_id,event_id='full-test')
        self.worker();self.assertEqual(self.receiver.requests,[]);self.assertEqual(self.effects(),[])
        self.worker(test=True);self.worker(test=True)
        state=self.client.test_dispatch_status('cpu','full-test')
        self.assertEqual(state['status'],'succeeded',state)
        marker=receipt['test_context']
        self.assertEqual(state['results']['run']['test_context'],marker)
        self.assertEqual(state['results']['notify']['alert']['test_context'],marker)
        self.assertEqual(self.receiver.requests[0][0],'/v1/test-alerts')
        self.assertEqual(self.receiver.requests[0][1]['test_context'],marker)
        self.assertEqual(self.effects(),[])
