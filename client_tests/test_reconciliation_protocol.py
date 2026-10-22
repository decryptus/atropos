import unittest
from atropos_client.protocol import arguments, MUTATIONS


class ReconciliationProtocolTests(unittest.TestCase):
    def test_confirmation_is_a_non_retriable_mutation(self):
        self.assertIn('reconcile', MUTATIONS)
        self.assertNotIn('plan_reconciliation', MUTATIONS)
        self.assertEqual(arguments('plan_reconciliation','cpu','event',mode='test'),
                         dict(name='cpu',event_id='event',mode='test'))
        args=dict(expected_state='a'*64,outcome='failed',evidence='external check',mode='real')
        self.assertEqual(arguments('reconcile','cpu','event',**args),dict(name='cpu',event_id='event',**args))
        with self.assertRaises(TypeError): arguments('reconcile','cpu','event',outcome='failed')
