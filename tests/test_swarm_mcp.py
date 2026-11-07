import json
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from pathlib import Path
from conductor.swarm_mcp import start_swarm

class Monitor:
    instance = 'i'
    def bridges(self): return {'a':{'instance_id':self.instance},'b':{'instance_id':'ib'}}
    def list_sessions(self,limit=50): return {'sessions':[{'id':'a'},{'id':'b'}]}

class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor = Monitor()
        self.enabled = True
        self.s = start_swarm(self.tmp.name,self.monitor,lambda:self.enabled)
    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()
    def post(self,path,body,token):
        req = urllib.request.Request(self.s.url.replace('/mcp',path),json.dumps(body).encode(),{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req) as response:
            data = response.read()
            return json.loads(data) if data else None
    def test_handshake_identity_and_revocation(self):
        token = self.post('/register',{'session_id':'a','instance_id':'i'},self.s.bootstrap_token)['token']
        premature = self.post('/mcp',{'jsonrpc':'2.0','id':0,'method':'tools/list'},token)
        self.assertEqual(premature['error']['code'],-32000)
        init = self.post('/mcp',{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},token)
        self.assertEqual(init['result']['protocolVersion'],'2025-03-26')
        self.assertIsNone(self.post('/mcp',{'jsonrpc':'2.0','method':'notifications/initialized'},token))
        listing = self.post('/mcp',{'jsonrpc':'2.0','id':2,'method':'tools/list'},token)
        self.assertEqual(len(listing['result']['tools']),10)
        msg = self.post('/mcp',{'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'swarm_send','arguments':{'to':'b','message':'hello','request_id':'r'}}},token)
        self.assertEqual(msg['result']['structuredContent']['sender'],'a')
        self.assertEqual(self.s.status()['connected_agents'][0]['session_id'],'a')
        self.monitor.instance = 'replacement'
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post('/mcp',{'jsonrpc':'2.0','id':4,'method':'ping'},token)
        self.assertEqual(raised.exception.code,401)
        raised.exception.close()
    def test_descriptor_disabled_and_registration(self):
        p = Path(self.tmp.name)/'endpoint.json'
        self.assertEqual(p.stat().st_mode & 0o777,0o600)
        self.assertNotIn(self.s.token,p.read_text())
        with self.assertRaises(urllib.error.HTTPError) as registration_error:
            self.post('/register',{'session_id':'conductor','instance_id':'i'},self.s.bootstrap_token)
        registration_error.exception.close()
        self.enabled = False
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post('/mcp',{'jsonrpc':'2.0','id':1,'method':'ping'},self.s.token)
        self.assertEqual(raised.exception.code,403)
        raised.exception.close()

    def test_codex_delivery_uses_authenticated_session_identity(self):
        token = self.post('/register', {'session_id':'a','instance_id':'i'}, self.s.bootstrap_token)['token']
        self.post('/mcp', {'jsonrpc':'2.0','id':1,'method':'initialize'}, token)
        self.post('/mcp', {'jsonrpc':'2.0','method':'notifications/initialized'}, token)
        arguments = {'message':'Please coordinate this task','request_id':'r1'}
        with patch('conductor.swarm_mcp.codex_call', return_value={'status':'queued'}) as deliver:
            response = self.post('/mcp', {'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':'codex_send','arguments':arguments}}, token)
            deliver.assert_called_once_with(self.s.broker.root, 'a', 'codex_send', arguments)
            self.assertEqual(response['result']['structuredContent']['status'], 'queued')
        forbidden = self.post('/mcp', {'jsonrpc':'2.0','id':3,'method':'tools/call',
            'params':{'name':'conductor_reply','arguments':{'message':'forged reply'}}}, token)
        self.assertTrue(forbidden['result']['isError'])

    def test_default_monitor_and_connection_freshness(self):
        from conductor.pi_monitor import PiMonitor
        with tempfile.TemporaryDirectory() as root:
            service = start_swarm(root)
            try:
                self.assertIsInstance(service.monitor,PiMonitor)
                self.assertIs(service.monitor,service.broker.monitor)
            finally:
                service.close()
        token = self.post('/register',{'session_id':'a','instance_id':'i'},self.s.bootstrap_token)['token']
        self.s.registrations[token]['last_seen'] -= 16
        self.assertEqual(self.s.status()['connected_agents'],[])
        self.enabled = False
        self.enabled = True
        self.post('/mcp',{'jsonrpc':'2.0','id':1,'method':'ping'},token)
        self.assertEqual(len(self.s.status()['connected_agents']),1)
