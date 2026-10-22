"""Redis delivery adapter. Durable execution authority stays in storage."""
import hashlib
from .definitions import name


class RedisDeliveryQueue:
    def __init__(self, client, namespace, lease_ms=30000):
        name(namespace, 'namespace')
        self.client, self.lease_ms = client, lease_ms
        self.prefix = namespace + ':delivery:'
        self.stream = self.prefix + 'stream'
        self.group = 'delivery-v1'
        self.cursor = '0-0'

    def send(self, delivery_id, run_id):
        # The mapping is removed on acknowledgment. Duplicate outbox delivery
        # cannot grant another claim; storage checks the current delivery ID.
        self.client.eval("""
            local old=redis.call('GET',KEYS[2])
            if old and #redis.call('XRANGE',KEYS[1],old,old)==1 then return old end
            local id=redis.call('XADD',KEYS[1],'*','run_id',ARGV[1],'delivery_id',ARGV[2])
            redis.call('SET',KEYS[2],id); return id
        """, 2, self.stream, self._key(delivery_id), run_id, delivery_id)

    def _key(self, delivery_id):
        return self.prefix + hashlib.sha256(delivery_id.encode()).hexdigest()

    def receive(self, consumer, block_ms):
        from redis.exceptions import ResponseError
        try:
            self.client.xgroup_create(self.stream, self.group, id='0', mkstream=True)
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc): raise
        claimed = self.client.xautoclaim(self.stream, self.group, consumer, self.lease_ms,
                                         start_id=self.cursor, count=1)
        self.cursor = claimed[0]
        rows = claimed[1]
        if not rows:
            batches = self.client.xreadgroup(self.group, consumer, {self.stream: '>'},
                                              count=1, **({'block': block_ms} if block_ms else {}))
            rows = batches[0][1] if batches else []
        if not rows: return None
        fields = rows[0][1]
        return fields['delivery_id'], {'run_id': fields['run_id']}

    def acknowledge(self, delivery_id):
        self.client.eval("""
            local id=redis.call('GET',KEYS[2])
            if id then redis.call('XACK',KEYS[1],ARGV[1],id); redis.call('XDEL',KEYS[1],id) end
            redis.call('DEL',KEYS[2]); return 1
        """, 2, self.stream, self._key(delivery_id), self.group)

    def close(self):
        self.client.close()
