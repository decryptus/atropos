"""One bounded Alertmanager POST, without redirects, proxies or retries."""
import http.client
import json
import ssl
import sys
from urllib.parse import urlsplit

from .alertmanager_adapter import API_PATH, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, validate_spec


def deliver(request):
    spec = validate_spec(request['configuration'])
    parsed = urlsplit(spec['origin'])
    if parsed.scheme == 'https':
        context = ssl.create_default_context(cadata=request['ca'])
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port,
            timeout=spec.get('timeout', 5), context=context)
    else:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=spec.get('timeout', 5))
    try:
        connection.request('POST', API_PATH, json.dumps(request['payload'], allow_nan=False).encode(),
            {'Authorization': 'Bearer ' + request['token'], 'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = connection.getresponse()
        # Alertmanager v2 specifies 200 for acceptance, not downstream delivery.
        if response.status != 200 or len(response.read(MAX_RESPONSE_BYTES + 1)) > MAX_RESPONSE_BYTES:
            raise ValueError('alertmanager_not_acknowledged')
    finally:
        connection.close()


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('invalid_alertmanager_request')
        deliver(json.loads(raw))
    except Exception:
        return 1
    sys.stdout.write('{"accepted":true}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
