"""Finite 24/48-hour observation sessions with conservative resume semantics.

This is a foreground diagnostic, not a production collector or a system service.
Resume preserves known identities but re-baselines both returned views: a pause
never becomes a fabricated continuous latency measurement.
"""
import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import time
from urllib.robotparser import RobotFileParser
from uuid import uuid4

import httpx

from app.ecb_observation_support import (LOCK_PATH,ObserverLock,atomic_json,read_json,read_journal)
from app.observe_ecb import (ECBObserver,Options,StopObservation,DeferredRound,
                            ROBOTS,USER_AGENT,speech_url)

ROOT=Path(__file__).resolve().parents[1]
REVIEW_REASONS={'access_denied','rate_limited','robots_unavailable_or_disallowed',
                'robots_unrecognized','schema_changed','response_unrecognized','redirect_or_http_error'}


@dataclass(frozen=True)
class SessionOptions(Options):
    rounds:int=1440
    duration:int=86400
    max_requests:int=6000
    max_details:int=20
    robots_refresh_seconds:int=3600

    def validate(self):
        values=vars(self)
        if any(type(value) is not int for value in values.values()):
            raise ValueError('Session limits must be integers')
        if not (1<=self.rounds<=2880 and 60<=self.interval<=300 and 10<=self.duration<=172800
                and 1<=self.max_requests<=12000 and 0<=self.max_details<=100
                and 60<=self.robots_refresh_seconds<=3600):
            raise ValueError('Session exceeds its round/time/request/robots limits')


def utc():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def timestamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('Timezone is required')
    return parsed.astimezone(timezone.utc)


def load_state(directory):
    directory=Path(directory).resolve()
    state=read_json(directory/'session.json')
    if state.get('schema_version')!=1 or not re.fullmatch(r'[a-f0-9]{32}',state.get('session_id','')):
        raise ValueError('Not a supported observation session')
    options=SessionOptions(**state['options']); options.validate()
    for segment in state['segments']:
        if not re.fullmatch(r'[a-f0-9]{32}',segment['segment_id']):raise ValueError('Invalid segment identity')
    if len(state['segments'])>100:raise ValueError('Too many resume segments')
    for key,limit in (('requests',options.max_requests),('round',options.rounds),('completed_rounds',options.rounds)):
        if type(state.get(key)) is not int or not 0<=state[key]<=limit:
            raise ValueError('Invalid session counters')
    timestamp(state['window_end_at'])
    if not math.isfinite(state['active_elapsed_seconds']) or not 0<=state['active_elapsed_seconds']<=options.duration+30:
        raise ValueError('Invalid elapsed time')
    return state,options


def recover_candidates(root,state):
    candidates=dict(state['candidates']); request_rows=0; partial=[]; recovered_counts=Counter()
    for segment in state['segments']:
        folder=Path(root)/'segments'/segment['segment_id']
        events,incomplete=read_journal(folder/'events.jsonl')
        if incomplete:partial.append(str(folder/'events.jsonl'))
        for event in events:
            if 'url' not in event:continue
            url=speech_url(event['url'])
            if (not isinstance(event.get('first_seen'),dict) or not isinstance(event.get('baseline_channels'),list)
                    or not isinstance(event.get('detail'),dict)):
                raise ValueError('Invalid event checkpoint')
            candidates[url]={key:event[key] for key in ('url','first_seen','baseline_channels','detail')}
        rows,incomplete=read_journal(folder/'requests.jsonl')
        if incomplete:partial.append(str(folder/'requests.jsonl'))
        request_rows+=sum('started_at' in row for row in rows)
        recovered_counts.update(row.get('state','unknown_request') for row in rows if 'started_at' in row)
    if len(candidates)>5000:raise ValueError('Candidate checkpoint limit exceeded')
    for url,event in candidates.items():
        if speech_url(url)!=url or event['url']!=url:raise ValueError('Invalid candidate identity')
        if not set(event['first_seen']).issubset({'rss','index'}):raise ValueError('Unknown channel')
        for observation in event['first_seen'].values():
            if not isinstance(observation.get('segment_id'),str):raise ValueError('Missing segment identity')
        if event['detail'].get('status')=='attempting':event['detail']['status']='interrupted_before_checkpoint'
    return candidates,max(state['requests'],request_rows),partial,recovered_counts


class SessionObserver(ECBObserver):
    def __init__(self,client,directory,options=None,*,resume=False,counter=time.monotonic,
                 sleep=time.sleep,now=lambda:datetime.now(timezone.utc),clock_report=None):
        self.session_ready=False
        self.root=Path(directory).resolve(); self.resuming=resume
        saved=None
        if resume:
            saved,options=load_state(self.root)
            if saved.get('stop_reason') in REVIEW_REASONS:
                raise ValueError('This session requires source/configuration review before further observation')
            if saved['round']>=options.rounds or saved['requests']>=options.max_requests:
                raise ValueError('Session budget is exhausted; old evidence is preserved')
            candidates,requests,partial,recovered_counts=recover_candidates(self.root,saved)
            if requests>=options.max_requests:raise ValueError('Recovered request budget is exhausted')
        else:
            options=options or SessionOptions(); options.validate()
            self.root.mkdir(parents=True,exist_ok=False)
        self.segment_id=uuid4().hex
        self.session_id=saved['session_id'] if saved else uuid4().hex
        self.created_at=saved['created_at'] if saved else now().astimezone(timezone.utc).isoformat()
        self.window_end_at=saved['window_end_at'] if saved else (now()+timedelta(seconds=options.duration)).astimezone(timezone.utc).isoformat()
        self.active_before=saved['active_elapsed_seconds'] if saved else 0
        self.segments=list(saved['segments']) if saved else []
        self.clock_evidence=saved.get('clock_evidence') if saved else None
        if clock_report:
            evidence=read_json(clock_report,256*1024)
            if evidence.get('mode')!='ecb_observer_clock_check':raise ValueError('Wrong clock evidence type')
            self.clock_evidence={'state':evidence.get('state','not_verified'),
                                 'checked_at':evidence.get('finished_at'),
                                 'clock_sync_verified':False,
                                 'maximum_absolute_offset_seconds':evidence.get('maximum_absolute_offset_seconds')}
        super().__init__(client,self.root/'segments'/self.segment_id,options,
                         counter=counter,sleep=sleep,now=now)
        # Guard the first request after releasing another observer's process lock.
        self.last_host=self.start
        if resume:
            # A full local cooldown also protects the endpoint interval across
            # a reboot or a wall-clock correction; no old monotonic value is used.
            self.last_host=self.start+options.interval-self.host_delay
        self.last_robots=None
        self.last_clock_pair=(self.counter(),self.now())
        self.clock_jump_seen=False
        if saved:
            self.candidates=candidates; self.requests=requests
            self.round=saved['round']; self.completed_rounds=saved['completed_rounds']
            self.counts=Counter(saved['request_states']); self.detail_checks=saved['detail_checks']
            for key,value in recovered_counts.items():self.counts[key]=max(self.counts[key],value)
            self.failures=list(saved['failures_and_coverage_gaps'])
            self.clock_jump_seen=any(item.get('state')=='local_clock_discontinuity' for item in self.failures)
            self.failures.append({'channel':'all','state':'resume_coverage_gap','round':self.round,
                                  'at':self.utc(),'previous_saved_at':saved['last_saved_at'],
                                  'partial_journals_ignored':partial,'segment_id':self.segment_id})
            # Cold HTTP caches and fresh per-channel baselines avoid made-up
            # absences, validators without bodies, and cross-process monotonic math.
            self.snapshots={}
        remaining_active=max(0,options.duration-self.active_before)
        remaining_wall=max(0,(timestamp(self.window_end_at)-self.now().astimezone(timezone.utc)).total_seconds())
        self.deadline=self.counter()+min(remaining_active,remaining_wall)
        self.segments.append({'segment_id':self.segment_id,'started_at':self.utc(),
                              'resumed':resume,'state':'running'})
        self.session_ready=True; self.checkpoint()

    def checkpoint(self):
        super().checkpoint()
        if not self.session_ready:return
        current=self.segments[-1]
        current.update(state=self.state,last_saved_at=self.utc(),stop_reason=self.reason,
                       elapsed_seconds=round(self.counter()-self.start,6))
        payload={'schema_version':1,'session_id':self.session_id,'created_at':self.created_at,
                 'window_end_at':self.window_end_at,'last_saved_at':self.utc(),
                 'state':self.state,'stop_reason':self.reason,'options':vars(self.options),
                 'active_elapsed_seconds':round(self.active_before+self.counter()-self.start,6),
                 'round':self.round,'completed_rounds':self.completed_rounds,'requests':self.requests,
                 'request_states':dict(self.counts),'detail_checks':self.detail_checks,
                 'candidates':self.candidates,'failures_and_coverage_gaps':self.failures,
                 'segments':self.segments,'clock_evidence':self.clock_evidence}
        atomic_json(self.root/'session.json',payload)
        heartbeat=read_json(self.directory/'heartbeat.json')
        atomic_json(self.root/'heartbeat.json',{**heartbeat,'pid':os.getpid(),
                    'session_id':self.session_id,'segment_id':self.segment_id})

    def check_stop(self):
        if self.session_ready:
            old_mono,old_wall=self.last_clock_pair
            mono,wall=self.counter(),self.now()
            discrepancy=(wall-old_wall).total_seconds()-(mono-old_mono)
            if abs(discrepancy)>2:
                self.clock_jump_seen=True
                self.failures.append({'channel':'all','state':'local_clock_discontinuity',
                                      'round':self.round,'at':self.utc(),'difference_seconds':round(discrepancy,6)})
                self.clock_evidence=None
            self.last_clock_pair=(mono,wall)
        super().check_stop()

    def refresh_robots(self):
        body,_=super().fetch('robots',ROBOTS)
        self.policy=body.decode('utf8',errors='strict')
        if 'user-agent:' not in self.policy.lower():raise StopObservation('robots_unrecognized')
        parser=RobotFileParser(); parser.parse(self.policy.splitlines())
        self.host_delay=max(5.0,float(parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 5))
        self.last_robots=self.counter()
        self.atomic('robots.json',{'checked_at':self.utc(),'url':ROBOTS,
                    'minimum_host_interval_seconds':self.host_delay,'text':self.policy})

    def fetch(self,key,url):
        if key!='robots':
            due=max(self.counter(),self.backoff_until,(self.last_host or self.start)+self.host_delay)
            if url in self.last_resource:due=max(due,self.last_resource[url]+self.options.interval)
            self.wait_until(due)
            if self.last_robots is None or self.counter()-self.last_robots>=self.options.robots_refresh_seconds:
                self.refresh_robots()
        return super().fetch(key,url)

    def log(self,name,data):
        if name=='events.jsonl':
            for observation in data.get('first_seen',{}).values():
                if 'segment_id' not in observation:
                    observation['segment_id']=self.segment_id
                    if self.resuming and observation.get('baseline'):
                        observation['baseline_reason']='resume_gap_rebaseline'
                        observation['gap_since_previous_view']=True
        super().log(name,{**data,'segment_id':self.segment_id})

    def report(self):
        report=super().report()
        report['comparisons']=[item for item in report['comparisons'] if
            len({value.get('segment_id') for value in self.candidates[item['url']]['first_seen'].values()})==1]
        report['paired_new_candidates']=len(report['comparisons'])
        report.update(observer_version='ecb_session_v1',mode='resumable_ecb_observation_session',
                      session_id=self.session_id,segment_id=self.segment_id,
                      session_created_at=self.created_at,window_end_at=self.window_end_at,
                      active_elapsed_seconds=round(self.active_before+self.counter()-self.start,3),
                      resume_startup_cooldown_seconds=self.options.interval if self.resuming else 5,
                      segments=self.segments,clock_evidence=self.clock_evidence,
                      clock_discontinuity_seen=self.clock_jump_seen,
                      resume_policy='cold_caches_preserved_ids_new_view_baselines_no_cross_gap_latency')
        try:
            age=(self.now().astimezone(timezone.utc)-timestamp(self.clock_evidence['checked_at'])).total_seconds()
        except (KeyError,TypeError,ValueError):age=None
        report['clock_evidence_age_seconds']=round(age,3) if age is not None else None
        report['clock_evidence_fresh']=age is not None and 0<=age<=3600
        report['notes'].append('中断后先重建两个入口基线，停机内补到的条目不算连续延迟样本；单调时间只在各自进程段内比较。')
        return report

    def run(self):
        try:
            self.phase='robots'; self.check_stop(); self.refresh_robots()
            first_round=self.counter(); initial_round=self.round
            for number in range(initial_round+1,self.options.rounds+1):
                self.phase='waiting_for_round'
                self.wait_until(first_round+(number-initial_round-1)*self.options.interval)
                self.round=number; self.phase='observing'; self.checkpoint()
                methods=(self.observe_rss,self.observe_index) if number%2 else (self.observe_index,self.observe_rss)
                try:
                    for method in methods:method()
                    self.completed_rounds+=1; self.check_details()
                except DeferredRound:self.phase='backoff'
                self.atomic('report.json',self.report()); self.checkpoint()
            self.state='completed_with_gaps' if self.failures else 'completed'; self.reason='round_limit'
        except DeferredRound:self.state,self.reason='stopped_with_gaps','robots_request_failed'
        except UnicodeError:self.state,self.reason='stopped','robots_unrecognized'
        except StopObservation as error:
            self.reason=str(error)
            self.state=('completed_with_gaps' if self.failures else 'completed') if self.reason=='duration_limit' else 'stopped'
        except KeyboardInterrupt:self.state,self.reason='interrupted','keyboard_interrupt'
        except Exception as error:
            self.state,self.reason='failed',type(error).__name__
        finally:
            self.phase='finished'; self.next_due=None
            self.checkpoint(); self.atomic('report.json',self.report())
            atomic_json(self.root/'report.json',self.report())
        return self.report()


def status(directory,lock_path=LOCK_PATH):
    root=Path(directory).resolve(); state,_=load_state(root)
    heartbeat=read_json(root/'heartbeat.json')
    with ObserverLock(lock_path) as lock:busy=lock.file is None
    owner_path=Path(lock_path).with_suffix('.owner.json')
    try:owner=read_json(owner_path)
    except (OSError,ValueError):owner={}
    active=busy and owner.get('session_directory')==str(root)
    age=(datetime.now(timezone.utc)-timestamp(heartbeat['heartbeat_at'])).total_seconds()
    return {'session_directory':str(root),'active':active,'any_observer_active':busy,
            'state':'running' if active and 0<=age<=45 else 'busy_or_stale' if active else state['state'],
            'phase':heartbeat['phase'],'heartbeat_at':heartbeat['heartbeat_at'],
            'heartbeat_age_seconds':round(age,3),'completed_rounds':state['completed_rounds'],
            'requests':state['requests'],'stop_reason':state['stop_reason'],
            'stop_file':heartbeat['stop_file']}


def request_stop(directory):
    root=Path(directory).resolve(); state,_=load_state(root)
    segment=state['segments'][-1]['segment_id']
    target=root/'segments'/segment/'STOP'
    target.touch(exist_ok=True)
    return {'state':'stop_requested','stop_file':str(target),'network_requested':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='ECB 长时观察准备与可恢复的前台会话，不改正式采集')
    sub=parser.add_subparsers(dest='action',required=True)
    start=sub.add_parser('run')
    duration=start.add_mutually_exclusive_group(required=True)
    duration.add_argument('--hours',type=int,choices=(24,48))
    duration.add_argument('--trial-seconds',type=int,choices=range(30,301),metavar='30..300')
    start.add_argument('--interval',type=int,default=60)
    start.add_argument('--rounds',type=int,help='可选轮数上限；仍受总时长和请求上限约束')
    start.add_argument('--max-details',type=int,default=20)
    start.add_argument('--robots-refresh-seconds',type=int,default=3600)
    start.add_argument('--clock-report',type=Path)
    start.add_argument('--output-dir',type=Path,default=ROOT/'.runtime/ecb-sessions')
    for action in ('resume','status','stop'):
        command=sub.add_parser(action); command.add_argument('--session',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='status':
            print(json.dumps(status(args.session),ensure_ascii=False,indent=2)); return 0
        if args.action=='stop':
            print(json.dumps(request_stop(args.session),ensure_ascii=False,indent=2)); return 0
        with ObserverLock() as ownership:
            if ownership.file is None:
                print(json.dumps({'state':'observer_busy','network_requested':False})); return 2
            if args.action=='run':
                seconds=args.hours*3600 if args.hours else args.trial_seconds
                if not 60<=args.interval<=300:raise ValueError('Interval must be 60-300 seconds')
                options=SessionOptions(rounds=args.rounds if args.rounds is not None else math.ceil(seconds/args.interval),interval=args.interval,
                         duration=seconds,max_requests=12000 if args.hours==48 else 6000 if args.hours else 40,
                         max_details=args.max_details,robots_refresh_seconds=args.robots_refresh_seconds)
                options.validate()
                root=args.output_dir/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
            else:root=args.session; options=None
            atomic_json(LOCK_PATH.with_suffix('.owner.json'),{'session_directory':str(root.resolve()),'pid':os.getpid()})
            with httpx.Client(follow_redirects=False,trust_env=False,timeout=15,
                              limits=httpx.Limits(max_keepalive_connections=0),headers={'User-Agent':USER_AGENT}) as client:
                observer=SessionObserver(client,root,options,resume=args.action=='resume',
                                          clock_report=getattr(args,'clock_report',None))
                print(json.dumps({'state':'starting','session_directory':str(observer.root),
                                  'segment_id':observer.segment_id,'stop_file':str(observer.stop_file)},ensure_ascii=False),flush=True)
                previous=signal.getsignal(signal.SIGTERM)
                def interrupt(*_):raise KeyboardInterrupt()
                signal.signal(signal.SIGTERM,interrupt)
                try:result=observer.run()
                finally:signal.signal(signal.SIGTERM,previous)
            print(json.dumps({key:result[key] for key in ('state','stop_reason','completed_rounds','requests',
                              'new_observed_candidates','paired_new_candidates')},ensure_ascii=False,indent=2))
            return 0 if result['state']=='completed' else 130 if result['state']=='interrupted' else 2
    except (ValueError,KeyError,TypeError,OSError) as error:
        print(json.dumps({'state':'configuration_or_evidence_error','error_type':type(error).__name__},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
