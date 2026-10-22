"""Execution pause for both SQL namespaces; admission stays bounded."""
from .definitions import name
from .maintenance import PAUSE_SUFFIX, MaintenanceError
from .test_execution import test_namespace
import uuid

BACKUP_SUFFIX = ':backup-operation'


class SQLExecutionMaintenance:
    def __init__(self, backend, namespace):
        name(namespace, 'namespace')
        self.backend, self.namespace = backend, namespace
        self.keys = [namespace+PAUSE_SUFFIX,test_namespace(namespace)+PAUSE_SUFFIX]
        self.operation_key = namespace + BACKUP_SUFFIX

    def pause(self):
        with self.backend.transaction():
            for key in self.keys: self.backend.set(key,'1')
        return self.status()

    def status(self):
        with self.backend.transaction():
            return dict(namespace=self.namespace,execution_paused=self.backend.get(self.keys[0]) is not None,
                test_execution_paused=self.backend.get(self.keys[1]) is not None,backup_operation=self.backend.get(self.operation_key),
                admissions='subject_to_capacity_and_authorization')

    def resume(self, *, reconciled=False):
        if reconciled is not True: raise MaintenanceError('reconciliation_confirmation_required')
        with self.backend.transaction():
            if self.backend.get(self.operation_key) is not None: raise MaintenanceError('backup_in_progress')
            self.backend.delete(*self.keys)
        return self.status()

    def begin_backup(self):
        operation = uuid.uuid4().hex
        with self.backend.transaction():
            if self.backend.get(self.operation_key) is not None: raise MaintenanceError('backup_in_progress')
            for key in self.keys: self.backend.set(key, '1')
            self.backend.set(self.operation_key, operation)
        return operation

    def end_backup(self, operation):
        if type(operation) is not str or len(operation) != 32:
            raise MaintenanceError('invalid_backup_operation')
        with self.backend.transaction():
            if self.backend.get(self.operation_key) != operation: raise MaintenanceError('backup_operation_changed')
            self.backend.delete(self.operation_key)

    def recover(self, operation, *, backup_stopped=False):
        if backup_stopped is not True: raise MaintenanceError('backup_stop_confirmation_required')
        self.end_backup(operation)
        return self.status()
