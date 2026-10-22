"""Single-attempt bounded child transport for the configured test receiver."""
import http.client
import hashlib
import json
import ssl
import sys
from urllib.parse import urlsplit

from .definitions import encode
from .test_alert_delivery import MAX_ACK_BYTES, MAX_ENVELOPE_BYTES, MAX_REQUEST_BYTES, RECEIVER_PATH, TEST_ID, validate_delivery


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def deliver(request):
    config, token, envelope = request['configuration'], request['token'], request['envelope']
    config = validate_delivery(config)
    marker = envelope.get('test_context', {})
    if (type(envelope.get('version')) is not int or envelope['version'] != 1
            or type(marker) is not dict or set(marker) != {'test', 'id'} or marker['test'] is not True
            or not isinstance(marker['id'], str) or TEST_ID.fullmatch(marker['id']) is None
            or envelope.get('run_id') != marker['id'] or not isinstance(envelope.get('step'), str)
            or envelope.get('delivery_id') != hashlib.sha256((envelope['run_id']+':'+envelope['step']).encode()).hexdigest()
            or envelope.get('alert', {}).get('test_context') != marker
            or envelope['alert']['test_context']['test'] is not True
            or envelope['alert'].get('test') is not True or envelope['alert'].get('test_id') != marker['id']):
        raise ValueError('test_context_required')
    parsed = urlsplit(config['url'])
    connection = (http.client.HTTPSConnection(parsed.hostname, parsed.port,
                    timeout=config['timeout'], context=ssl.create_default_context(cadata=request.get('ca')))
                  if parsed.scheme == 'https' else
                  http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=config['timeout']))
    try:
        body = encode(envelope, MAX_ENVELOPE_BYTES).encode()
        connection.request('POST', RECEIVER_PATH, body, {
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'Authorization': 'Bearer ' + token,
            'X-Atropos-Test': 'true', 'X-Atropos-Test-ID': envelope['test_context']['id'],
            'Idempotency-Key': envelope['delivery_id']})
        response = connection.getresponse()
        # No redirects, proxies, HTTP authentication negotiation or retries.
        if response.status not in (200, 202) or response.getheader('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise ValueError('invalid_test_acknowledgement')
        raw = response.read(MAX_ACK_BYTES + 1)
        if len(raw) > MAX_ACK_BYTES:
            raise ValueError('invalid_test_acknowledgement')
        ack = json.loads(raw, object_pairs_hook=unique_object)
        if (type(ack) is not dict or set(ack) != {'version', 'accepted', 'test_context', 'delivery_id'}
                or type(ack['version']) is not int or ack['version'] != 1 or ack['accepted'] is not True
                or type(ack['test_context']) is not dict or set(ack['test_context']) != {'test', 'id'}
                or ack['test_context']['test'] is not True
                or ack['test_context'] != envelope['test_context']
                or ack['delivery_id'] != envelope['delivery_id']):
            raise ValueError('invalid_test_acknowledgement')
    finally:
        connection.close()


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('invalid_test_request')
        deliver(json.loads(raw))
    except Exception:
        return 1  # Never output remote bodies, credentials or exception text.
    sys.stdout.write('{"accepted":true}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
