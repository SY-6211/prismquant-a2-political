import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from app.check_ecb_coverage import check_manifest,coverage
from app.check_observer_clock import clock_check,parse_samples
from app.ecb_observation_support import ObserverLock,atomic_json,read_json,read_journal
from app.observe_ecb_session import (SessionObserver,SessionOptions,load_state,recover_candidates,
                                     request_stop,status)
from app.observe_ecb import ROBOTS,RSS,VERSIONS
from tests.test_ecb_observer import Clock,OLD,NEW,OTHER,HEADER,rss,chunk,speech


class ECBSessionTests(unittest.TestCase):
    def setup_session(self,folder,handler=None,options=None):
        clock=Clock(); calls=[]; owner={}
        def transport(req):
            observer=owner['observer']; calls.append((clock(),observer.round,str(req.url)))
            if handler:
                value=handler(req,observer)
                if value is not None:return value
            if str(req.url)==ROBOTS:return httpx.Response(200,text='User-agent: *\nDisallow:\nCrawl-delay: 5')
            headers={'ETag':'"v1"'}
            if req.headers.get('if-none-match'):return httpx.Response(304,headers=headers)
            if str(req.url)==RSS:return httpx.Response(200,text=rss([OLD]),headers=headers)
            if str(req.url)==VERSIONS:return httpx.Response(200,json=[{'version':'1','hash':'abc'}],headers=headers)
            if req.url.path.endswith('metadata.json'):
                return httpx.Response(200,json={'header':HEADER,'sort':['!pub_timestamp'],'chunk_size':250,'total_records':1},headers=headers)
            if req.url.path.endswith('chunk_0.json'):return httpx.Response(200,json=chunk([OLD]),headers=headers)
            return httpx.Response(200,text=speech())
        client=httpx.Client(transport=httpx.MockTransport(transport),follow_redirects=False)
        self.addCleanup(client.close)
        observer=SessionObserver(client,Path(folder)/'session',options or SessionOptions(rounds=3,duration=240,max_requests=40),
                                 counter=clock,sleep=clock.sleep,now=clock.now)
        owner['observer']=observer
        return observer,clock,calls,owner,client

    def stop_after_baseline(self,observer,clock):
        def sleep(seconds):
            clock.sleep(seconds)
            if clock()>=35:observer.stop_file.touch()
        observer.sleep=sleep
        self.assertEqual(observer.run()['stop_reason'],'stop_file')

    def test_long_limits_are_available_without_changing_finite_defaults(self):
        SessionOptions(rounds=2880,duration=172800,max_requests=12000).validate()
        for options in (SessionOptions(rounds=2881),SessionOptions(duration=172801),
                        SessionOptions(interval=30),SessionOptions(robots_refresh_seconds=3601),
                        SessionOptions(rounds=True)):
            with self.assertRaises(ValueError):options.validate()

    def test_robots_refreshes_and_revalidates_before_expired_policy_is_used(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls,_,_=self.setup_session(folder,options=SessionOptions(rounds=3,duration=300,max_requests=40,robots_refresh_seconds=60))
            self.assertEqual(observer.run()['state'],'completed')
            times=[t for t,_,url in calls if url==ROBOTS]
            self.assertGreaterEqual(len(times),3)
            self.assertTrue(all(b-a>=60 for a,b in zip(times,times[1:])))

    def test_new_robots_denial_stops_further_source_access(self):
        def handler(req,observer):
            if str(req.url)==ROBOTS and observer.round>=2:
                return httpx.Response(200,text='User-agent: *\nDisallow: /rss/')
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls,_,_=self.setup_session(folder,handler,SessionOptions(rounds=3,duration=300,max_requests=40,robots_refresh_seconds=60))
            self.assertEqual(observer.run()['stop_reason'],'robots_unavailable_or_disallowed')
            self.assertEqual(sum(url==RSS for _,_,url in calls),1)

    def test_resume_preserves_ids_and_excludes_downtime_arrivals(self):
        def handler(req,observer):
            if not observer.resuming:return None
            if str(req.url)==RSS:return httpx.Response(200,text=rss([NEW,OLD]))
            if req.url.path.endswith('chunk_0.json'):return httpx.Response(200,json=chunk([NEW,OLD]))
        with tempfile.TemporaryDirectory() as folder:
            first,clock,calls,owner,client=self.setup_session(folder,handler)
            self.stop_after_baseline(first,clock)
            root=first.root; first_segment=first.segment_id
            clock.sleep(10)
            second=SessionObserver(client,root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)
            owner['observer']=second
            prior_call_count=len(calls); resumed_at=clock()
            report=second.run()
            self.assertGreaterEqual(calls[prior_call_count][0]-resumed_at,60)
            self.assertEqual(report['new_observed_candidates'],0)
            self.assertEqual(report['paired_new_candidates'],0)
            self.assertEqual(len(second.candidates),2)
            self.assertEqual(second.candidates[OLD]['first_seen']['index']['segment_id'],first_segment)
            observation=second.candidates[NEW]['first_seen']['index']
            self.assertEqual(observation['baseline_reason'],'resume_gap_rebaseline')
            self.assertIsNone(observation['last_absent_view_request_started_at'])
            self.assertTrue(any(g['state']=='resume_coverage_gap' for g in report['failures_and_coverage_gaps']))
            self.assertTrue((root/'segments'/first_segment/'STOP').exists())
            self.assertNotEqual(second.segment_id,first_segment)

    def test_resume_recovers_journal_when_candidate_checkpoint_was_lost(self):
        with tempfile.TemporaryDirectory() as folder:
            first,clock,_,owner,client=self.setup_session(folder)
            self.stop_after_baseline(first,clock)
            state=read_json(first.root/'session.json'); state['candidates']={}
            atomic_json(first.root/'session.json',state)
            second=SessionObserver(client,first.root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)
            owner['observer']=second
            self.assertIn(OLD,second.candidates)
            self.assertEqual(second.requests,5)
            self.assertEqual(second.run()['new_observed_candidates'],0)

    def test_partial_journal_tail_is_retained_and_reported_as_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            first,clock,_,owner,client=self.setup_session(folder)
            self.stop_after_baseline(first,clock)
            journal=first.directory/'events.jsonl'
            with journal.open('ab') as handle:handle.write(b'{"state":')
            before=journal.read_bytes()
            second=SessionObserver(client,first.root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)
            owner['observer']=second
            self.assertEqual(journal.read_bytes(),before)
            self.assertTrue(second.failures[-1]['partial_journals_ignored'])
            self.assertIn(OLD,second.candidates)

    def test_interior_corruption_and_escaped_segment_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            first,clock,_,_,client=self.setup_session(folder)
            self.stop_after_baseline(first,clock)
            journal=first.directory/'events.jsonl'
            journal.write_bytes(b'{broken}\n{}\n')
            with self.assertRaises(ValueError):SessionObserver(client,first.root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)
            state=read_json(first.root/'session.json'); state['segments'][0]['segment_id']='../outside'
            atomic_json(first.root/'session.json',state)
            with self.assertRaises(ValueError):load_state(first.root)

    def test_resume_keeps_total_request_budget_and_permission_stops_require_review(self):
        with tempfile.TemporaryDirectory() as folder:
            first,clock,_,_,client=self.setup_session(folder,options=SessionOptions(rounds=3,duration=240,max_requests=1))
            self.assertEqual(first.run()['stop_reason'],'request_limit')
            with self.assertRaises(ValueError):SessionObserver(client,first.root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)
        with tempfile.TemporaryDirectory() as folder:
            def handler(req,_):
                if str(req.url)==RSS:return httpx.Response(429)
            first,clock,_,_,client=self.setup_session(folder,handler)
            self.assertEqual(first.run()['stop_reason'],'rate_limited')
            with self.assertRaises(ValueError):SessionObserver(client,first.root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)

    def test_stop_command_prevents_any_network_and_status_uses_lock_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls,_,_=self.setup_session(folder)
            result=request_stop(observer.root)
            self.assertEqual(result['state'],'stop_requested')
            self.assertEqual(observer.run()['stop_reason'],'stop_file'); self.assertEqual(calls,[])
            lock_path=Path(folder)/'observer.lock'
            self.assertFalse(status(observer.root,lock_path)['active'])
            with ObserverLock(lock_path) as lock:
                self.assertIsNotNone(lock.file)
                atomic_json(lock_path.with_suffix('.owner.json'),{'session_directory':str(observer.root)})
                self.assertTrue(status(observer.root,lock_path)['active'])
                with ObserverLock(lock_path) as second:self.assertIsNone(second.file)

    def test_clock_jump_is_visible_and_never_becomes_verified_public_time(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,clock,_,_,_=self.setup_session(folder)
            observer.now=lambda:clock.now()+timedelta(seconds=20 if clock()>=30 else 0)
            report=observer.run()
            self.assertTrue(report['clock_discontinuity_seen'])
            self.assertEqual(report['local_clock_sync'],'not_verified')
            self.assertEqual(report['publication_latency'],'not_measured')

    def test_completed_session_cannot_be_silently_extended(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,clock,_,_,client=self.setup_session(folder,options=SessionOptions(rounds=1,duration=240,max_requests=40))
            observer.run(); before=(observer.root/'session.json').read_bytes()
            with self.assertRaises(ValueError):SessionObserver(client,observer.root,resume=True,counter=clock,sleep=clock.sleep,now=clock.now)
            self.assertEqual(before,(observer.root/'session.json').read_bytes())

    def test_clock_check_is_read_only_and_does_not_claim_sync(self):
        commands=[]
        def runner(command,**_):
            commands.append(command)
            if '/query' in command:return SimpleNamespace(returncode=1,stdout='error (0x80070426)',stderr='')
            return SimpleNamespace(returncode=0,stdout='RdtscStart,RdtscEnd,FileTime,RoundtripDelay,NtpOffset\n1,2,3,0.1,0.2\n2,3,4,0.1,-0.1\n3,4,5,0.1,0.05\n',stderr='')
        result=clock_check(probe_ntp=True,runner=runner,platform='nt')
        self.assertEqual(result['state'],'offset_estimate_only')
        self.assertFalse(result['clock_sync_verified']); self.assertFalse(result['clock_or_service_changed'])
        self.assertEqual(result['service_query']['error_codes'],['0x80070426'])
        self.assertEqual(len(commands),2)
        self.assertFalse(any(any(arg in ('/resync','/config','start') for arg in cmd) for cmd in commands))

    def test_clock_errors_and_large_or_nonfinite_offsets_do_not_pass(self):
        self.assertEqual(parse_samples('1,2,3,0.2,nan\n1,2,3,9,0\n'),[])
        def fail(command,**_):raise subprocess.TimeoutExpired(command,25)
        self.assertEqual(clock_check(probe_ntp=True,runner=fail,platform='nt')['state'],'not_verified')
        def large(command,**_):return SimpleNamespace(returncode=0,stdout='1,2,3,0.1,9\n'*3,stderr='')
        self.assertEqual(clock_check(probe_ntp=True,runner=large,platform='nt')['state'],'large_offset_estimate')
        self.assertEqual(clock_check(platform='posix')['state'],'not_verified')

    def manifest(self,entries):
        return {'schema_version':1,'window':{'since':'2026-11-05T00:00:00Z','until':'2026-11-06T00:00:00Z'},'entries':entries}

    def test_no_text_calendar_entries_are_excluded_without_100_percent_claim(self):
        manifest=self.manifest([{'label':'Calendar appearance','review_status':'scheduled_no_text',
                                'publication_url':None,'reference_url':'https://www.ecb.europa.eu/press/calendars/weekly/html/index.en.html'}])
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_,_,_=self.setup_session(folder); observer.run()
            path=Path(folder)/'manifest.json'; atomic_json(path,manifest)
            result=coverage(observer.root,path)
            self.assertEqual(result['expected_text_targets'],0)
            self.assertIsNone(result['selected_reference_match_ratio'])
            self.assertEqual(result['state'],'no_eligible_text_targets')

    def test_coverage_only_compares_selected_verified_urls_without_network(self):
        entries=[{'label':'Known','review_status':'confirmed_text','publication_url':url,
                  'reference_url':url,'reference_seen_at':'2026-11-05T00:30:00Z','speaker_confirmed':True}
                 for url in (OLD,OTHER)]
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls,_,_=self.setup_session(folder); observer.run(); before=len(calls)
            path=Path(folder)/'manifest.json'; atomic_json(path,self.manifest(entries))
            result=coverage(observer.root,path)
            self.assertEqual(result['selected_reference_match_ratio'],0.5)
            self.assertEqual(len(calls),before)
            self.assertEqual(result['independent_completeness'],'not_verified')

    def test_bad_reference_urls_dates_and_duplicate_identity_are_rejected(self):
        base={'label':'Example','review_status':'confirmed_text','publication_url':OLD,'reference_url':OLD,
              'reference_seen_at':'2026-11-05T00:30:00Z','speaker_confirmed':True}
        for override in ({'reference_url':'https://evil.example/'},{'publication_url':'https://evil.example/'},
                         {'reference_seen_at':'2026-11-05T00:30:00'},{'speaker_confirmed':False}):
            with self.subTest(override=override),self.assertRaises(ValueError):check_manifest(self.manifest([{**base,**override}]))
        with self.assertRaises(ValueError):check_manifest(self.manifest([base,base]))

    def test_reference_outside_observation_window_does_not_get_a_match_percentage(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_,_,_=self.setup_session(folder); observer.run()
            reference={'schema_version':1,'window':{'since':'2026-11-07T00:00:00Z','until':'2026-11-08T00:00:00Z'},
                'entries':[{'label':'Known URL in another window','review_status':'confirmed_text','publication_url':OLD,
                            'reference_url':OLD,'reference_seen_at':'2026-11-07T00:30:00Z','speaker_confirmed':True}]}
            path=Path(folder)/'manifest.json'; atomic_json(path,reference)
            result=coverage(observer.root,path)
            self.assertEqual(result['state'],'no_observation_overlap')
            self.assertIsNone(result['selected_reference_match_ratio'])
            self.assertEqual(result['observed_selected_targets'],0)

    def test_old_clock_evidence_expires_in_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_,_,_=self.setup_session(folder)
            observer.clock_evidence={'state':'offset_estimate_only','checked_at':'2026-11-04T00:00:00Z'}
            self.assertFalse(observer.report()['clock_evidence_fresh'])


if __name__=='__main__':unittest.main()
