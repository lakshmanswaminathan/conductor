import unittest
from unittest.mock import patch
from conductor import pi_processes as processes

RAW = ''' 10 1 ttys001 S Tue Nov  3 10:00:00 2026 /bin/zsh
20 10 ttys001 S Tue Nov  3 10:01:00 2026 pi
30 20 ?? S Tue Nov  3 10:02:00 2026 /bin/sh
40 30 ?? R Tue Nov  3 10:03:00 2026 /usr/bin/python3
50 10 ?? S Tue Nov  3 10:04:00 2026 pi
60 1 ?? Z Tue Nov  3 10:05:00 2026 pi
70 1 ttys002 S Tue Nov  3 10:05:00 2026 /some path/My Tool
'''

class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.records = processes.parse_snapshot(RAW)
    def test_parse_bounded_public_fields(self):
        self.assertEqual(len(processes.parse_snapshot(RAW,2)),2)
        self.assertEqual(self.records[2]['tty'],None)
        self.assertEqual(self.records[6]['executable'],'My Tool')
        self.assertEqual(set(self.records[0]),{'pid','ppid','tty','stat','started_at','executable'})
        self.assertEqual(processes.normalize_tty('/dev/ttys001'),'ttys001')
    def test_snapshot_no_arguments_or_environment_collection(self):
        with patch.object(processes.subprocess,'run') as run:
            run.return_value.stdout = RAW
            self.assertEqual(processes.snapshot(),self.records)
            command = run.call_args.args[0]
            self.assertEqual(command[-1],'pid=,ppid=,tty=,stat=,lstart=,comm=')
    def test_shell_descendants_and_detached_child(self):
        self.assertEqual([r['pid'] for r in processes.descendants(self.records,20)],[30,40])
        self.assertEqual([r['pid'] for r in processes.ancestors(self.records,40)],[30,20,10])
        self.assertEqual(processes.descendants(self.records,20,'stale'),[])
    def test_terminal_lineage_and_ambiguity(self):
        terminals = [{'tty':'/dev/ttys001','surface':'a'}]
        binding = processes.terminal_binding(self.records,40,terminals)
        self.assertEqual((binding['status'],binding['via'],binding['source_pid']),('confirmed','ancestor',20))
        self.assertEqual(processes.terminal_binding(self.records,20,terminals)['via'],'process')
        terminals.append({'tty':'ttys001','surface':'b'})
        self.assertEqual(processes.terminal_binding(self.records,20,terminals)['status'],'ambiguous')
        self.assertEqual(processes.terminal_binding(self.records,20,terminals,'old')['status'],'stale')
    def test_pid_reuse_and_unobserved_pi(self):
        matching = processes.identity(self.records[1])
        self.assertEqual([r['pid'] for r in processes.unobserved_pi(self.records,[matching])],[50])
        result = processes.unobserved_pi(self.records,[{'pid':20,'started_at':'old'},{'pid':50}])
        self.assertEqual([r['reason'] for r in result],['observer_identity_stale','observer_identity_unverified'])
    def test_cycles_are_bounded(self):
        cycle = [dict(self.records[0],ppid=20),self.records[1]]
        self.assertEqual(len(processes.ancestors(cycle,20)),1)
        self.assertEqual(len(processes.descendants(cycle,20)),1)
