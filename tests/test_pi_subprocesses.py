"""Regressions for subprocess discovery and truthful terminal dispatch receipts."""
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from conductor.pi_sessions import PiSessions
from conductor.pi_monitor import PiMonitor, context_digest
from conductor.computer import cmux_call, perform
from conductor.pi_monitor import perform as pi_perform


class SubprocessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bridge = self.root / 'bridge'
        self.bridge.mkdir(mode=0o700)
        self.monitor = PiMonitor(PiSessions(self.root / 'default'), self.bridge)
        self.monitor._inventory = {'terminals': [{'workspace': 'w1', 'surface': 's1', 'tty': 'tty1'}]}
        self.monitor._inventory_at = time.monotonic()

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, sid, **values):
        data = dict(version=1, session_id=sid, observed_at=time.time(), status='closed', pid=os.getpid(), **values)
        (self.bridge / (sid + '.json')).write_text(json.dumps(data))
        return data

    def test_custom_subprocess_trace_survives_exit_and_parent_attribution(self):
        path = self.root / 'custom.jsonl'
        path.write_text(json.dumps({'type':'session','id':'child','cwd':'/project','parentSession':'/parent.jsonl'})+'\n'+
                        json.dumps({'type':'message','id':'p1','parentId':None,'message':{'role':'user','content':'Review the patch'}})+'\n')
        self.record('child', session_file=str(path))
        with patch.object(self.monitor, 'processes', return_value=[]):
            listing = self.monitor.list_sessions()
            view = self.monitor.read_session('child')
        self.assertEqual(listing['sessions'][0]['id'], 'child')
        self.assertEqual(view['session']['parent_session_file'], '/parent.jsonl')
        self.assertEqual(view['session']['harness']['status'], 'recorded')
        self.assertEqual(view['context']['messages'][0]['text'], 'Review the patch')
        # Registration must not lend another file its requested identity.
        self.record('wrong', session_file=str(path))
        with patch.object(self.monitor, 'processes', return_value=[]):
            self.assertNotEqual(self.monitor.read_session('wrong')['session']['trace_source'], 'session_file')

    def test_memory_only_child_has_public_context_and_process_relationships(self):
        at=time.time(); started=datetime.fromtimestamp(at).strftime('%a %b %d %H:%M:%S %Y')
        entries=[{'type':'message','id':'p1','parentId':None,'message':{'role':'user','content':'Child task'}},
                 {'type':'message','id':'p2','parentId':'p1','message':{'role':'assistant','content':[{'type':'text','text':'Child result'},{'type':'thinking','thinking':'PRIVATE'}]}}]
        self.record('child', session_file=None, recent_entries=entries, recent_entries_truncated=True)
        bridges={'parent':{'pid':11,'process_started_at':at}, 'child':{'pid':13,'process_started_at':at,'leaf_id':'p2','status':'idle'}}
        processes=[{'pid':p,'ppid':parent,'started_at':started,'executable':exe,'tty':tty,'stat':'S'} for p,parent,exe,tty in [(11,1,'pi','tty1'),(12,11,'sh',None),(13,12,'pi',None)]]
        with patch.object(self.monitor, 'bridges', return_value=bridges), patch.object(self.monitor, 'processes', return_value=processes):
            view=self.monitor.read_session('child')
        self.assertEqual(view['session']['parent_session_ids'], ['parent'])
        self.assertEqual(view['session']['trace_source'], 'observer_snapshot')
        self.assertEqual(view['context']['messages'][-1]['text'], 'Child result')
        self.assertTrue(view['context']['truncated'])
        self.assertNotIn('PRIVATE',json.dumps(view))
        self.assertEqual(context_digest(view)['parent_session_ids'], ['parent'])

    def test_recycled_process_identity_does_not_attach_children(self):
        process={'pid':11,'ppid':1,'tty':'tty1','stat':'S','executable':'pi','started_at':'Wed Nov 04 10:00:00 2026'}
        bridge={'pid':11,'status':'idle','process_started_at':datetime(2026,11,3,10).timestamp()}
        with patch.object(self.monitor, 'processes', return_value=[process]):
            _,missing=self.monitor.inventory({'old':bridge},[])
        self.assertEqual(missing[0]['reason'],'observer_identity_stale')
        self.assertNotIn('subprocesses',self.monitor.enrich({'id':'old'},bridge,[],[process],{'old':bridge}))

    def test_large_catalog_pages_without_hiding_unattached_processes(self):
        for i in range(12):
            self.record('session-'+str(i), recent_entries=[{'type':'message','id':'prompt','parentId':None,
                        'message':{'role':'user','content':'Task details '*1000}}])
        process={'pid':999,'ppid':1,'executable':'pi','tty':'tty1','started_at':'now','stat':'S'}
        store=Mock();store.config.return_value={'computer_access':True}
        ids=[];offset=0
        with patch('conductor.pi_monitor.monitor',self.monitor), patch.object(self.monitor,'processes',return_value=[process]):
            while True:
                result=pi_perform(store,{'action':'list','offset':offset})
                self.assertLess(len(json.dumps(result)),6000)
                self.assertEqual(result['unobserved_processes'][0]['pid'],999)
                ids.extend(s['id'] for s in result['sessions'])
                offset=result['next_offset']
                if offset is None:break
        self.assertEqual(len(set(ids)),12)
        self.assertEqual(len(ids),12)

    @patch('conductor.runtime.cmux_binary', return_value='/cmux')
    @patch('conductor.computer.subprocess.run')
    def test_send_distinguishes_inserted_from_submitted(self, run, binary):
        run.return_value=Mock(returncode=0,stdout='OK',stderr='')
        receipt=cmux_call('send',workspace='w1',surface='s1',text='echo safe')
        self.assertFalse(receipt['submitted'])
        self.assertEqual(receipt['execution_status'],'not_submitted')
        self.assertEqual(run.call_count,1)
        receipt=cmux_call('send',workspace='w1',surface='s1',text='echo safe',submit=True)
        self.assertTrue(receipt['submitted'])
        self.assertEqual(receipt['execution_status'],'unverified')
        self.assertEqual(run.call_count,3)

    def test_pi_beyond_process_preview_still_blocks_terminal_input(self):
        records=[{'pid':i,'ppid':0,'tty':'tty1','executable':'sh' if i<51 else 'pi','started_at':'now','stat':'S'} for i in range(1,52)]
        tree={'terminals':[{'workspace':'w1','surface':'s1','tty':'tty1'}]}
        original=cmux_call
        def calls(action, **kwargs):
            if action=='tree': return tree
            return original(action, **kwargs)
        with patch('conductor.pi_processes.snapshot',return_value=records), patch('conductor.computer.cmux_call',side_effect=calls):
            result=original('processes',workspace='w1',surface='s1')
        self.assertEqual(len(result['processes']),50)
        self.assertTrue(result['contains_pi'])
        store=Mock(); store.config.return_value={'computer_access':True}
        with patch('conductor.computer.cmux_call',return_value=result) as call:
            with self.assertRaisesRegex(ValueError,'Pi agent'):
                perform(store,'cmux',{'action':'send','workspace':'w1','surface':'s1','text':'do work'})
            self.assertEqual(call.call_count,1)

if __name__=='__main__': unittest.main()
