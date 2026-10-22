import unittest
from atropos_client.protocol import arguments,MUTATIONS

class InterventionSearchProtocolTests(unittest.TestCase):
    def test_read_only_finite_signature(self):
        self.assertNotIn('search_interventions',MUTATIONS)
        args=arguments('search_interventions','cpu',query='web',mode='test',since=0,page=2)
        self.assertEqual(args,dict(name='cpu',query='web',mode='test',since=0,page=2))
        with self.assertRaises(TypeError):arguments('search_interventions','cpu',subject='bob')
