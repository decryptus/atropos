"""Explicit commands. The core never invokes this module."""
import argparse
import asyncio
import json
import sys
from pathlib import Path


from .storage_config import backend_settings
from .backend_registry import open_storage, open_queue, backend_errors
from .storage_backup import BackupError
from .maintenance import MaintenanceError
from .actions import builtin_actions
from .definitions import DefinitionError, MAX_DEFINITION_BYTES, load
from .storage_contract import Conflict, QueueFull
from .service import WorkflowService
from .worker import Worker
from .rule_cli import add_rule_parser, execute_rule_command


def parser():
    result = argparse.ArgumentParser(prog='atropos')
    result.add_argument('--namespace', default='atropos')
    commands = result.add_subparsers(dest='command', required=True)
    add_rule_parser(commands)
    for command in ('validate', 'plan', 'submit'):
        item = commands.add_parser(command)
        item.add_argument('workflow')
        if command == 'submit':
            item.add_argument('--input', help='JSON input file; defaults to an empty object')
            item.add_argument('--target', required=True)
            item.add_argument('--dedup-key')
    worker = commands.add_parser('worker')
    worker.add_argument('--once', action='store_true', help='attempt one step per worker')
    worker.add_argument('--concurrency', type=int, default=1)
    service = commands.add_parser('rules-serve', help='Atropos-owned rule API')
    service.add_argument('--config', required=True)
    service.add_argument('--port', type=int, default=8082)
    for command in ('storage-pause', 'storage-resume', 'storage-status', 'storage-backup-recover'):
        item = commands.add_parser(command, help='control execution maintenance; admission capacity remains separate')
        item.add_argument('--config', required=True)
        if command == 'storage-resume':
            item.add_argument('--confirm-reconciled', action='store_true')
        if command == 'storage-backup-recover':
            item.add_argument('--operation', required=True)
            item.add_argument('--confirm-backup-stopped', action='store_true')
    for command in ('storage-backup', 'storage-backup-inspect', 'storage-restore'):
        item = commands.add_parser(command, help='trusted-host quiescent storage backup administration')
        if command != 'storage-backup-inspect':
            item.add_argument('--config', required=True)
        item.add_argument('--output' if command == 'storage-backup' else '--input', required=True)
        if command == 'storage-restore':
            item.add_argument('--confirm-source-stopped', action='store_true')
    retention = commands.add_parser('storage-retention', help='inspect a private archive offline; no purge or storage connection')
    retention.add_argument('--input', required=True)
    adapters = commands.add_parser('execution-adapters', help='list trusted adapter revisions; no credentials or contact')
    adapters.add_argument('--config', required=True)
    bindings = commands.add_parser('rules-bindings', help='list server-approved dispatch binding IDs')
    bindings.add_argument('--config', required=True)
    guarded = commands.add_parser('rules-worker', help='execute approved rule dispatches with current authorization')
    guarded.add_argument('--config', required=True)
    guarded.add_argument('--once', action='store_true')
    guarded.add_argument('--test', action='store_true', help='isolated test queue; registered simulators only')
    for command in ('queue-status', 'queue-configure'):
        item = commands.add_parser(command, help='inspect or configure the shared outstanding-run limit')
        item.add_argument('--test', action='store_true', help='use the isolated test namespace')
        if command == 'queue-configure':
            item.add_argument('--max-outstanding', type=int, required=True, help='0 pauses new admissions; never evicts existing runs')
    commands.add_parser('status').add_argument('run_id')
    item = commands.add_parser('resolve', description=(
        'Terminate an uncertain run after independently verifying the external '
        'outcome and that no action remains in flight. Does not resume later steps.'))
    item.add_argument('run_id')
    item.add_argument('--outcome', choices=['succeeded', 'failed'], required=True)
    return result


async def run_workers(store, actions, args):
    workers = [Worker(store, actions) for _ in range(args.concurrency)]
    await asyncio.gather(*(worker.run_once(0) if args.once else worker.run()
                           for worker in workers))


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'rules':
        return execute_rule_command(args)
    client = None
    try:
        if args.command == 'storage-retention':
            from .retention import inspect_archive
            report = inspect_archive(args.input)
            print(json.dumps(report, indent=2))
            return 1 if report['attention_required'] else 0
        if args.command in ('storage-pause', 'storage-resume', 'storage-status', 'storage-backup-recover'):
            from .rule_operator import load_operator_config
            from contextlib import closing
            document, _ = load_operator_config(args.config, remote=True)
            with closing(open_storage(document['storage'])) as storage:
                control = storage.maintenance(document['namespace'])
                if args.command == 'storage-pause': result = control.pause()
                elif args.command == 'storage-resume': result = control.resume(reconciled=args.confirm_reconciled)
                elif args.command == 'storage-status': result = control.status()
                else: result = control.recover(args.operation, backup_stopped=args.confirm_backup_stopped)
                print(json.dumps(result, indent=2))
            return 0
        if args.command in ('storage-backup', 'storage-backup-inspect', 'storage-restore'):
            from .storage_backup import backup, configured_storage, read_backup, restore, summary
            if args.command == 'storage-backup-inspect':
                result = summary(read_backup(args.input))
            else:
                if args.command == 'storage-restore' and not args.confirm_source_stopped:
                    raise ValueError('source_stop_confirmation_required')
                with configured_storage(args.config) as (storage, namespace):
                    result = (backup(storage, namespace, args.output)
                        if args.command == 'storage-backup' else restore(storage, namespace,
                            read_backup(args.input), source_stopped=args.confirm_source_stopped))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'execution-adapters':
            from .rule_operator import load_operator_config
            from .execution_adapters import adapter_revision
            document, _ = load_operator_config(args.config, remote=True)
            print(json.dumps({key: {'backend': spec['backend'], 'target': spec['target'],
                'revision': adapter_revision(spec), 'test_supported': spec.get('simulation') in ('noop', 'test_receiver')}
                for key, spec in document.get('execution_adapters', {}).items()}, indent=2))
            return 0
        if args.command == 'rules-bindings':
            from .rule_execution import list_bindings
            print(json.dumps(list_bindings(args.config), indent=2))
            return 0
        if args.command == 'rules-worker':
            from .rule_execution import run_rule_worker
            run_rule_worker(args.config, once=args.once, test_mode=args.test)
            return 0
        if args.command == 'rules-serve':
            from .rule_server import serve
            serve(args.config, args.port)
            return 0
        actions = builtin_actions()
        if args.command in ('validate', 'plan'):
            plan = WorkflowService(None, actions).plan(load(args.workflow))
            print(json.dumps(plan if args.command == 'plan' else {'valid': True}, indent=2))
            return 0
        if args.command == 'worker' and not 1 <= args.concurrency <= 32:
            raise ValueError('concurrency must be 1–32')
        # Explicit local default; credentials, if needed, come from the operator.
        settings = backend_settings()
        client = open_storage(settings['storage'])
        namespace = args.namespace
        if args.command in ('queue-status', 'queue-configure') and args.test:
            from .test_execution import test_namespace
            namespace = test_namespace(namespace)
        store = client.workflow_store(namespace, open_queue(settings['queue'], namespace))
        if args.command in ('queue-status', 'queue-configure'):
            result = (store.configure_queue(args.max_outstanding) if args.command == 'queue-configure'
                      else store.queue_status())
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'submit':
            inputs = {}
            if args.input:
                with Path(args.input).open('rb') as stream:
                    raw = stream.read(MAX_DEFINITION_BYTES + 1)
                if len(raw) > MAX_DEFINITION_BYTES:
                    raise DefinitionError('input file too large')
                inputs = json.loads(raw)
            state = WorkflowService(store, actions).submit(
                load(args.workflow), inputs, args.target, args.dedup_key)
            print(json.dumps({'id': state['id'], 'status': state['status']}))
        elif args.command == 'worker':
            store.initialize()
            asyncio.run(run_workers(store, actions, args))
        elif args.command == 'status':
            print(json.dumps(store.get(args.run_id), indent=2))
        else:
            print(json.dumps(store.resolve(args.run_id, args.outcome), indent=2))
        return 0
    except (BackupError, MaintenanceError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except QueueFull:
        print('Queue capacity reached; new run was not admitted.', file=sys.stderr)
        return 2
    except (DefinitionError, Conflict):
        print('Invalid definition or conflicting operation.', file=sys.stderr)
        return 2
    except (ValueError, KeyError, OSError, TypeError, RecursionError):
        print('Invalid input or unavailable local file/run.', file=sys.stderr)
        return 2
    except ImportError:
        print('Optional dependencies required; install the documented service/executor extras.', file=sys.stderr)
        return 2
    except backend_errors():
        print('Backend operation failed; check configuration and retained run status.', file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
