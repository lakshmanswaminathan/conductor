import concurrent.futures
import tempfile
import unittest
from conductor.swarm import SwarmBroker

class Monitor:
    def bridges(self): return {'a':{},'b':{}}

class SwarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = SwarmBroker(self.tmp.name,Monitor())
    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()
    def test_restart_idempotency_auth_and_redaction(self):
        args = {'to':'b','message':'hello sk-abcdefghijk','request_id':'r'}
        msg = self.b.call('a','swarm_send',args)
        self.b.close()
        self.b = SwarmBroker(self.tmp.name,Monitor())
        self.assertEqual(msg,self.b.call('a','swarm_send',args))
        self.assertEqual(msg['message'],'hello [redacted]')
        for bad in [dict(args,message='changed'),dict(args,sender='spoof')]:
            with self.assertRaises(ValueError): self.b.call('a','swarm_send',bad)
        self.assertEqual(self.b.call('a','swarm_inbox',{})['messages'],[])
        with self.assertRaises(ValueError): self.b.call('a','swarm_ack',{'message_id':msg['id']})
        self.assertFalse(self.b.call('b','swarm_ack',{'message_id':msg['id']})['completed'])
    def test_causal_limit(self):
        parent = None
        for i in range(5):
            sender,recipient = ('a','b') if i%2 == 0 else ('b','a')
            args = {'to':recipient,'message':'ping','request_id':str(i)}
            if parent: args['parent_id'] = parent
            msg = self.b.call(sender,'swarm_send',args)
            self.b.call(recipient,'swarm_ack',{'message_id':msg['id']})
            parent = msg['id']
        with self.assertRaises(ValueError):
            self.b.call('b','swarm_send',{'to':'a','message':'ping','request_id':'six','parent_id':parent})
    def test_atomic_claim_dependencies_owner(self):
        self.b.call('a','swarm_task',{'request_id':'create-t','action':'create','task_id':'t','title':'Task'})
        other = SwarmBroker(self.tmp.name,Monitor())
        def claim(pair):
            broker,actor = pair
            try:
                broker.call(actor,'swarm_task',{'request_id':'claim-t','action':'claim','task_id':'t'})
                return actor
            except ValueError: return None
        with concurrent.futures.ThreadPoolExecutor() as pool:
            winners = [v for v in pool.map(claim,[(self.b,'a'),(other,'b')]) if v]
        other.close()
        self.assertEqual(len(winners),1)
        self.b.call('a','swarm_task',{'request_id':'create-child','action':'create','task_id':'child','title':'Child','dependencies':['t']})
        with self.assertRaises(ValueError): self.b.call('a','swarm_task',{'request_id':'claim-child','action':'claim','task_id':'child'})
        with self.assertRaises(ValueError): self.b.call('stranger','swarm_task',{'request_id':'complete-t','action':'update','task_id':'t','status':'completed'})
        self.b.call(winners[0],'swarm_task',{'request_id':'complete-t','action':'update','task_id':'t','status':'completed'})
        self.b.call('a','swarm_task',{'request_id':'claim-child','action':'claim','task_id':'child'})

    def test_task_requires_retry_key(self):
        with self.assertRaises(ValueError):
            self.b.call('a','swarm_task',{'action':'create','task_id':'x','title':'x'})
        args = {'action':'create','task_id':'x','title':'x','request_id':'create-x'}
        self.assertEqual(self.b.call('a','swarm_task',args),self.b.call('a','swarm_task',args))
