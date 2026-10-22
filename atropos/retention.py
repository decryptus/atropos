"""Offline archive capacity report. Never opens storage or decides what to delete."""
import base64
import hashlib

from .storage_backup import (canonical, decode_backup, MAX_FILE_BYTES, MAX_DATA_BYTES,
                             MAX_KEYS, prefixes)
from .sql_backup import MAX_ROWS, SAFE_STATUSES

REPORT_VERSION = 1
WARNING_PERCENT = 80
REDIS_CATEGORIES = {'run:': 'runs', 'target:': 'target_locks', 'rules:': 'rule_records'}
POLICY = 'retain_all_evidence'


def capacity(used, limit, unit):
    return dict(used=used, limit=limit, remaining=max(0, limit-used), unit=unit,
                warning=used*100 >= limit*WARNING_PERCENT)


def inspect_archive(path):
    from atropos_client.files import read_private
    raw = read_private(path, MAX_FILE_BYTES)
    payload = decode_backup(raw)
    sql = 'backend' in payload
    report = dict(version=REPORT_VERSION, backend=payload.get('backend', 'redis'),
        namespace=payload['namespace'], captured_at_ms=payload['captured_at_ms'],
        archive_sha256=hashlib.sha256(canonical(payload)).hexdigest(),
        warning_percent=WARNING_PERCENT, policy=POLICY, purge_supported=False,
        restore_verified=False, capacity={}, real={}, test={})
    report['capacity']['archive_bytes'] = capacity(len(raw), MAX_FILE_BYTES, 'bytes')
    namespaces = dict(zip(prefixes(payload['namespace']), ('real', 'test')))
    if sql:
        tables = payload['tables']
        report['table_rows'] = {key: len(rows) for key, rows in tables.items()}
        report['capacity']['rows'] = capacity(sum(report['table_rows'].values()), MAX_ROWS, 'rows')
        report['capacity']['payload_bytes'] = capacity(len(canonical(payload)), MAX_DATA_BYTES, 'bytes')
        for mode in namespaces.values():
            report[mode] = dict(runs=0, statuses={status: 0 for status in sorted(SAFE_STATUSES)}, admissions=0)
        for row in tables['runs']:
            mode = namespaces[row[0]]; status = row[3]
            if type(status) is not str or status not in SAFE_STATUSES:
                raise ValueError('invalid_retention_run_status')
            report[mode]['runs'] += 1; report[mode]['statuses'][status] += 1
        for row in tables['admissions']:
            report[namespaces[row[0]]]['admissions'] += 1
    else:
        entries = payload['entries']; size = 0
        for mode in namespaces.values():
            report[mode] = dict(keys=0, runs=0, target_locks=0, rule_records=0, other=0, statuses=None)
        for entry in entries:
            # Redis DUMP is opaque. Never deserialize it or infer run outcomes.
            ns = next(ns for ns in namespaces if entry['key'].startswith(ns+':'))
            mode = report[namespaces[ns]]; mode['keys'] += 1
            tail = entry['key'][len(ns)+1:]
            category = next((label for prefix,label in REDIS_CATEGORIES.items() if tail.startswith(prefix)), 'other')
            mode[category] += 1
            size += len(entry['key'].encode()) + len(base64.b64decode(entry['dump'], validate=True))
        report['capacity']['keys'] = capacity(len(entries), MAX_KEYS, 'keys')
        report['capacity']['dump_bytes'] = capacity(size, MAX_DATA_BYTES, 'bytes')
    report['attention_required'] = any(item['warning'] for item in report['capacity'].values())
    return report
