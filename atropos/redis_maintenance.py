"""Standalone storage adapter for the generic execution-maintenance contract."""
import uuid

from .definitions import name
from .maintenance import MaintenanceError, PAUSE_SUFFIX
from .test_execution import test_namespace


BACKUP_SUFFIX = ':backup-operation'


class RedisExecutionMaintenance:
    def __init__(self, client, namespace):
        name(namespace, 'namespace')
        self.client, self.namespace = client, namespace
        self.keys = [namespace + PAUSE_SUFFIX, test_namespace(namespace) + PAUSE_SUFFIX,
                     namespace + BACKUP_SUFFIX]

    def pause(self):
        self.client.eval("redis.call('SET',KEYS[1],'1'); redis.call('SET',KEYS[2],'1'); return 1", 2, *self.keys[:2])
        return self.status()

    def status(self):
        values = self.client.mget(self.keys)
        operation = values[2]
        if isinstance(operation, bytes): operation = operation.decode('ascii')
        return {'namespace': self.namespace, 'execution_paused': values[0] is not None,
                'test_execution_paused': values[1] is not None, 'backup_operation': operation,
                'admissions': 'subject_to_capacity_and_authorization'}

    def resume(self, *, reconciled=False):
        if reconciled is not True:
            raise MaintenanceError('reconciliation_confirmation_required')
        result = self.client.eval("""
            if redis.call('EXISTS',KEYS[3]) == 1 then return 0 end
            redis.call('DEL',KEYS[1],KEYS[2]); return 1
            """, 3, *self.keys)
        if not result: raise MaintenanceError('backup_in_progress')
        return self.status()

    def begin_backup(self):
        operation = uuid.uuid4().hex
        result = self.client.eval("""
            if redis.call('EXISTS',KEYS[3]) == 1 then return 0 end
            redis.call('SET',KEYS[1],'1'); redis.call('SET',KEYS[2],'1')
            redis.call('SET',KEYS[3],ARGV[1]); return 1
            """, 3, *self.keys, operation)
        if not result: raise MaintenanceError('backup_in_progress')
        return operation

    def end_backup(self, operation):
        if not isinstance(operation, str) or len(operation) != 32:
            raise MaintenanceError('invalid_backup_operation')
        result = self.client.eval("""
            if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end
            redis.call('DEL',KEYS[1]); return 1
            """, 1, self.keys[2], operation)
        if not result: raise MaintenanceError('backup_operation_changed')

    def recover(self, operation, *, backup_stopped=False):
        if backup_stopped is not True:
            raise MaintenanceError('backup_stop_confirmation_required')
        self.end_backup(operation)
        # This only releases the backup operator fence. Execution stays paused.
        return self.status()
