"""Bounded ECB index/RSS comparison, separate from production and the database.

Snapshots describe returned views, not proof of when the origin first published
an event. The initial view of EACH channel is a baseline, including after a
channel's failed startup. Publication-to-browser latency is never inferred here.
"""
import argparse
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import time
from urllib.robotparser import RobotFileParser
from uuid import uuid4
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException

from app.history_sources import ECB_INDEX, parse_ecb_chunk
from app.preview_ecb import parse_feed_items
from app.preview_ecb_speech import parse_speech, speech_url
from app.probe_fast_sources import HEADER_NAMES, USER_AGENT, index_base, robots_allows

RSS = 'https://www.ecb.europa.eu/rss/press.html'
VERSIONS = ECB_INDEX + 'versions.json'
ROBOTS = 'https://www.ecb.europa.eu/robots.txt'
MAX_BYTES = 8 * 1024 * 1024
PARSER_ERRORS = (ValueError, KeyError, TypeError, IndexError, OverflowError,
                 ParseError, DefusedXmlException)


@dataclass(frozen=True)
class Options:
    rounds: int = 3
    interval: int = 60
    duration: int = 240
    max_requests: int = 40
    max_details: int = 3

    def validate(self):
        if not (1 <= self.rounds <= 30 and 60 <= self.interval <= 300
                and 10 <= self.duration <= 1800 and 1 <= self.max_requests <= 160
                and 0 <= self.max_details <= 10):
            raise ValueError('Use 1-30 rounds, interval 60-300s, duration 10-1800s, '
                             '1-160 requests and 0-10 new detail checks')


class StopObservation(Exception):
    pass


class DeferredRound(Exception):
    pass


class ECBObserver:
    def __init__(self, client, directory, options=None, *, counter=time.monotonic,
                 sleep=time.sleep, now=lambda: datetime.now(timezone.utc), stop_file=None):
        self.options = options or Options()
        self.options.validate()
        self.client, self.counter, self.sleep, self.now = client, counter, sleep, now
        self.directory = Path(directory)
        # Never replace evidence from another run, including an interrupted run.
        self.directory.mkdir(parents=True, exist_ok=False)
        self.stop_file = Path(stop_file) if stop_file else self.directory / 'STOP'
        self.start = counter()
        self.started_at = self.utc()
        self.deadline = self.start + self.options.duration
        self.last_host = None
        self.last_resource = {}
        self.policy = None
        self.host_delay = 5.0
        self.backoff_until = self.start
        self.errors = 0
        self.cache = OrderedDict()
        self.snapshots = {}
        self.candidates = {}
        self.failures = []
        self.detail_checks = 0
        self.pending_details = []
        self.requests = 0
        self.counts = Counter()
        self.round = 0
        self.completed_rounds = 0
        self.index_version = None
        self.pending_index_version = None
        self.index_items = None
        self.index_coverage = None
        self.state = 'running'
        self.phase = 'starting'
        self.reason = None
        self.next_due = None
        self.checkpoint()

    def utc(self):
        return self.now().astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    def stamp(self):
        return {'at': self.utc(), 'elapsed_seconds': round(self.counter()-self.start, 6)}

    def atomic(self, name, data):
        target = self.directory / name
        temporary = target.with_suffix(target.suffix + '.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        temporary.replace(target)

    def log(self, name, data):
        with (self.directory / name).open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(data, ensure_ascii=False, allow_nan=False)+'\n')
            handle.flush()

    def checkpoint(self):
        self.atomic('heartbeat.json', {
            'state': self.state, 'phase': self.phase, 'started_at': self.started_at,
            'heartbeat_at': self.utc(), 'round': self.round,
            'completed_rounds': self.completed_rounds, 'requests': self.requests,
            'next_due_in_seconds': None if self.next_due is None else round(max(0, self.next_due-self.counter()), 2),
            'stop_file': str(self.stop_file), 'stop_reason': self.reason,
        })

    def check_stop(self):
        if self.stop_file.exists():
            raise StopObservation('stop_file')
        if self.counter() >= self.deadline:
            raise StopObservation('duration_limit')

    def wait_until(self, due):
        self.next_due = due
        last_heartbeat = self.counter()-10
        while self.counter() < due:
            self.check_stop()
            if self.counter()-last_heartbeat >= 10:
                self.checkpoint(); last_heartbeat = self.counter()
            self.sleep(min(1.0, due-self.counter(), max(.001, self.deadline-self.counter())))
        self.next_due = None
        self.check_stop()

    def allowed(self, url):
        if url in (RSS, VERSIONS, ROBOTS):
            return True
        base = self.pending_index_version or self.index_version
        if base and url in (base+'metadata.json', base+'data/0/chunk_0.json'):
            return True
        try:
            return speech_url(url) == url
        except ValueError:
            return False

    def fetch(self, key, url):
        self.check_stop()
        if not self.allowed(url):
            raise ValueError('Outside the observer URL allowlist')
        if key != 'robots' and (not self.policy or not robots_allows(self.policy, url)):
            self.log('requests.jsonl', {'key':key, 'url':url, 'round':self.round,
                                       'state':'robots_disallowed', **self.stamp()})
            raise StopObservation('robots_unavailable_or_disallowed')
        if self.requests >= self.options.max_requests:
            raise StopObservation('request_limit')
        due = max(self.counter(), self.backoff_until)
        if self.last_host is not None:
            due = max(due, self.last_host+self.host_delay)
        if url in self.last_resource:
            due = max(due, self.last_resource[url]+self.options.interval)
        self.phase = 'waiting_for_request'; self.wait_until(due)
        cached = self.cache.get(url)
        headers = cached['validators'] if cached else {}
        record = {'key':key, 'url':url, 'round':self.round,
                  'started_at':self.utc(), 'started_elapsed_seconds':round(self.counter()-self.start, 6),
                  'conditional':bool(headers)}
        started = self.counter()
        self.last_host = started; self.last_resource[url] = started
        self.requests += 1; self.phase = 'requesting'; self.checkpoint()
        body = bytearray()
        try:
            with self.client.stream('GET', url, headers=headers,
                                    timeout=min(15.0, max(.1, self.deadline-started))) as response:
                status = response.status_code
                record.update(status=status, headers={k:response.headers[k][:1024]
                              for k in HEADER_NAMES if k in response.headers})
                if status in (401, 403, 429):
                    record['state'] = 'access_or_rate_stop'
                    raise StopObservation('rate_limited' if status == 429 else 'access_denied')
                if status >= 500 or status == 408:
                    record['state'] = 'transient_http_error'
                    raise DeferredRound()
                if status == 304:
                    if not cached or not headers:
                        raise ValueError('304 without a previously parsed response and validator')
                    data = cached['body']; record['state'] = 'not_modified'
                    record['bytes'] = 0
                elif status == 200:
                    for chunk in response.iter_bytes(65536):
                        self.check_stop(); body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise ValueError('Body limit exceeded')
                    data = bytes(body)
                    etag, modified = response.headers.get('etag'), response.headers.get('last-modified')
                    validators = ({'If-None-Match':etag} if etag else
                                  {'If-Modified-Since':modified} if modified else {})
                    self.cache[url] = {'body':data, 'validators':validators}
                    self.cache.move_to_end(url)
                    while len(self.cache) > 8:
                        self.cache.popitem(last=False)
                    record.update(state='received', bytes=len(data))
                else:
                    record['state'] = 'unexpected_http_status'
                    raise StopObservation('redirect_or_http_error')
                record['body_sha256'] = hashlib.sha256(data).hexdigest()
                self.errors = 0
        except (httpx.HTTPError, DeferredRound) as error:
            record.setdefault('state', 'transport_error')
            record['error_type'] = type(error).__name__
            self.errors += 1
            delay = min(300, self.options.interval * 2**min(self.errors-1, 3))
            self.backoff_until = self.counter()+delay
            record['backoff_seconds'] = delay
            self.failures.append({'channel':key, 'at':self.utc(), 'round':self.round,
                                  'state':record['state'], 'error_type':record['error_type']})
            raise DeferredRound() from None
        except ValueError as error:
            record.update(state='response_unrecognized', error_type=type(error).__name__)
            raise StopObservation('response_unrecognized') from None
        finally:
            record.setdefault('state', 'interrupted_request')
            record.update(finished_at=self.utc(), finished_elapsed_seconds=round(self.counter()-self.start, 6),
                          request_ms=round((self.counter()-started)*1000, 3))
            self.counts[record['state']] += 1
            self.log('requests.jsonl', record)
            # Do not propagate response cookies into subsequent public requests.
            self.client.cookies.clear()
            self.checkpoint()
        return data, record

    def parse_error(self, channel, error):
        self.log('snapshots.jsonl', {'channel':channel, 'round':self.round,
                                    'state':'schema_unrecognized', 'error_type':type(error).__name__, **self.stamp()})
        raise StopObservation('schema_changed') from None

    def observe_rss(self):
        body, record = self.fetch('rss', RSS)
        try:
            items = []
            feed_items = parse_feed_items(body)
            for item in feed_items:
                if not (item.get('title') or '').lower().startswith('christine lagarde:'):
                    continue
                try:
                    url = speech_url(item.get('url') or '')
                except ValueError:
                    continue
                items.append({**item, 'url':url})
        except PARSER_ERRORS as error:
            self.parse_error('rss', error)
        self.snapshot('rss', items, record, {'scope':'current_rss_window', 'all_feed_items':len(feed_items)})

    def observe_index(self):
        body, record = self.fetch('index_versions', VERSIONS)
        try:
            base = index_base(body)
            changed = base != self.index_version or self.index_items is None
            if changed:
                self.pending_index_version = base
                metadata_body, _ = self.fetch('index_metadata', base+'metadata.json')
                metadata = json.loads(metadata_body)
                if (metadata.get('sort', [None])[0] != '!pub_timestamp'
                        or type(metadata.get('chunk_size')) is not int
                        or not 1 <= metadata['chunk_size'] <= 1000
                        or type(metadata.get('total_records')) is not int
                        or metadata['total_records'] < 1):
                    raise ValueError('Index schema or sort order changed')
                chunk, record = self.fetch('index_chunk', base+'data/0/chunk_0.json')
                items, stamps = parse_ecb_chunk(chunk, metadata['header'])
                if not stamps or stamps != sorted(stamps, reverse=True) or len(stamps) > metadata['chunk_size']:
                    raise ValueError('Index window is empty or not ordered')
                self.index_items = items
                self.index_coverage = {'scope':'first_index_chunk_only', 'rows':len(stamps),
                                       'total_records':metadata['total_records'],
                                       'oldest_reported_time':min(stamps), 'newest_reported_time':max(stamps)}
                self.index_version = base
        except PARSER_ERRORS as error:
            self.parse_error('index', error)
        finally:
            self.pending_index_version = None
        self.snapshot('index', self.index_items, record,
                      {**self.index_coverage, 'index_version':self.index_version,
                       'unchanged_version_reuses_window':not changed})

    def snapshot(self, channel, items, record, coverage):
        by_url = {}
        for item in items:
            url = speech_url(item['url'])
            value = {k:item.get(k) for k in ('title','published_at','published_raw')}
            if url in by_url and by_url[url] != value:
                self.parse_error(channel, ValueError('Conflicting candidate identity'))
            by_url[url] = value
        previous = self.snapshots.get(channel)
        first_snapshot = previous is None
        newly_observed = []
        for url, item in by_url.items():
            event = self.candidates.setdefault(url, {'url':url, 'baseline_channels':[],
                                                     'first_seen':{}, 'detail':{'status':'not_requested'}})
            if channel in event['first_seen']:
                continue
            if first_snapshot:
                event['baseline_channels'].append(channel)
            absence = previous if previous and url not in previous['urls'] else None
            event['first_seen'][channel] = {
                'at':record['finished_at'], 'elapsed_seconds':record['finished_elapsed_seconds'],
                'baseline':first_snapshot, 'candidate':item,
                'last_absent_view_request_started_at':absence['started_at'] if absence else None,
                'first_present_view_request_finished_at':record['finished_at'],
                'observed_view_interval_seconds':round(record['finished_elapsed_seconds']-absence['started_elapsed_seconds'],6) if absence else None,
                'absence_scope':'returned_window_only_not_origin_publication',
                'reported_time_status':'reported_only_not_verified_public_time',
                'gap_since_previous_view':any(f['round'] >= previous['round'] for f in self.failures) if previous else False,
            }
            try:
                published = datetime.fromisoformat((item.get('published_at') or '').replace('Z','+00:00'))
                before_start = published < datetime.fromisoformat(self.started_at.replace('Z','+00:00'))
            except ValueError:
                before_start = None
            event['first_seen'][channel]['reported_published_before_run'] = before_start
            self.log('events.jsonl', {'state':'baseline_candidate' if first_snapshot else 'new_channel_candidate',
                                    'channel':channel, 'round':self.round, **event})
            if not first_snapshot:
                newly_observed.append(url)
        self.snapshots[channel] = {
            'urls':set(by_url), 'round':self.round, 'started_at':record['started_at'],
            'started_elapsed_seconds':record['started_elapsed_seconds'],
            'finished_at':record['finished_at'], 'coverage':coverage,
        }
        self.log('snapshots.jsonl', {'state':'baseline' if first_snapshot else 'observed',
                                    'channel':channel, 'round':self.round, 'candidates':len(by_url),
                                    'new_channel_candidates':len(newly_observed),
                                    'candidate_urls':sorted(by_url), 'coverage':coverage,
                                    'request_started_at':record['started_at'], 'checked_at':record['finished_at']})
        # Read BOTH discovery channels before slower attribution checks so that
        # optional detail requests do not favour whichever channel runs first.
        self.pending_details.extend(newly_observed)

    def check_details(self):
        while self.pending_details:
            url = self.pending_details.pop(0)
            event = self.candidates[url]
            if (event['baseline_channels'] or event['detail']['status'] != 'not_requested'
                    or self.detail_checks >= self.options.max_details):
                continue
            self.detail_checks += 1
            event['detail']['status'] = 'attempting'
            try:
                body, _ = self.fetch('new_candidate_detail', url)
                detail = parse_speech(body, url)
                event['detail'] = {k:detail.get(k) for k in ('verification_status','speaker','speaker_evidence','page_date','title','body_characters')}
                event['detail']['status'] = detail['verification_status']
            except DeferredRound:
                event['detail'] = {'status':'request_failed_with_backoff'}
                raise
            except (StopObservation, KeyboardInterrupt):
                event['detail'] = {'status':'interrupted_or_stopped'}
                raise
            except PARSER_ERRORS as error:
                event['detail'] = {'status':'schema_unrecognized','error_type':type(error).__name__}
                self.parse_error('detail', error)
            finally:
                self.log('events.jsonl', {'state':'detail_check','round':self.round, **event})

    def report(self):
        comparisons = []
        new_events = []
        for event in self.candidates.values():
            if not event['baseline_channels']:
                new_events.append(event)
                seen = event['first_seen']
                if set(seen) == {'rss','index'}:
                    comparisons.append({'url':event['url'],
                        'rss_minus_index_first_seen_seconds':round(seen['rss']['elapsed_seconds']-seen['index']['elapsed_seconds'],6),
                        'meaning':'positive means index observed first; polling order and cache affect this number'})
        return {
            'mode':'bounded_ecb_comparison_observer', 'observer_version':'ecb_comparison_v1',
            'state':self.state, 'stop_reason':self.reason,
            'started_at':self.started_at, 'finished_at':self.utc(),
            'elapsed_seconds':round(self.counter()-self.start,3),
            'options':vars(self.options), 'rounds_started':self.round, 'completed_rounds':self.completed_rounds,
            'requests':self.requests, 'request_states':dict(self.counts),
            'channels_initialized':sorted(self.snapshots),
            'baseline_candidates':sum(bool(e['baseline_channels']) for e in self.candidates.values()),
            'new_observed_candidates':len(new_events), 'new_events':new_events,
            'paired_new_candidates':len(comparisons), 'comparisons':comparisons,
            'detail_checks':self.detail_checks, 'failures_and_coverage_gaps':self.failures,
            'last_successful_views':{k:{n:v for n,v in s.items() if n != 'urls'} for k,s in self.snapshots.items()},
            'sample_conclusion':'insufficient_new_event_samples' if not comparisons else 'event_observations_only_not_percentile_acceptance',
            'publication_latency':'not_measured', 'public_to_browser_latency':'not_measured',
            'independent_coverage':'not_verified', 'local_clock_sync':'not_verified',
            'database_accessed':False, 'production_changed':False, 'models_called':False,
            'notes':[
                '首次快照是既有数据，每个渠道分别初始化；初始失败后的首次成功也不是新事件样本。',
                '缺席/出现区间仅描述该入口返回窗口，不代表原站首次公开时间；索引仅检查首块，RSS可能滚动淘汰旧条目。',
                '两入口属于同站，不能相互证明完整性；首次发现先后受轮询错位和缓存影响。',
                '未知公开时间、时钟同步和浏览器时间不补造；不计算发布延迟P95/P99。',
            ],
        }

    def run(self):
        try:
            self.phase = 'robots'
            body, _ = self.fetch('robots', ROBOTS)
            self.policy = body.decode('utf-8', errors='strict')
            if 'user-agent:' not in self.policy.lower():
                raise StopObservation('robots_unrecognized')
            parser = RobotFileParser(); parser.parse(self.policy.splitlines())
            self.host_delay = max(5.0, float(parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 5))
            self.atomic('robots.json', {'checked_at':self.utc(), 'url':ROBOTS,
                                       'minimum_host_interval_seconds':self.host_delay, 'text':self.policy})
            first_round = self.counter()
            for number in range(1, self.options.rounds+1):
                self.phase = 'waiting_for_round'
                self.wait_until(first_round+(number-1)*self.options.interval)
                self.round = number; self.phase = 'observing'; self.checkpoint()
                methods = (self.observe_rss, self.observe_index) if number % 2 else (self.observe_index, self.observe_rss)
                try:
                    for method in methods:
                        method()
                    self.completed_rounds += 1
                    self.check_details()
                except DeferredRound:
                    self.phase = 'backoff'
                self.atomic('report.json', self.report()); self.checkpoint()
            self.state = 'completed_with_gaps' if self.failures else 'completed'
            self.reason = 'round_limit'
        except DeferredRound:
            self.state, self.reason = 'stopped_with_gaps', 'robots_request_failed'
        except UnicodeError:
            self.state, self.reason = 'stopped', 'robots_unrecognized'
        except StopObservation as error:
            self.state, self.reason = 'stopped', str(error)
        except KeyboardInterrupt:
            self.state, self.reason = 'interrupted', 'keyboard_interrupt'
        finally:
            self.phase = 'finished'; self.next_due = None
            self.atomic('report.json', self.report()); self.checkpoint()
        return self.report()


def main(argv=None):
    parser = argparse.ArgumentParser(description='ECB 索引/RSS 有界对照观察，不入库、不调用模型')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--duration', type=int, default=240, help='总时长上限，秒')
    parser.add_argument('--max-requests', type=int, default=40)
    parser.add_argument('--max-details', type=int, default=3)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parents[1]/'.runtime/ecb-observer')
    parser.add_argument('--stop-file', type=Path)
    args = parser.parse_args(argv)
    options = Options(args.rounds, args.interval, args.duration, args.max_requests, args.max_details)
    try:
        options.validate()
    except ValueError as error:
        parser.error(str(error))
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    directory = args.output_dir/run_id
    print(json.dumps({'state':'starting', 'evidence_directory':str(directory),
                      'stop_file':str(args.stop_file or directory/'STOP')}, ensure_ascii=False), flush=True)
    from app.ecb_observation_support import LOCK_PATH, ObserverLock, atomic_json
    with ObserverLock() as ownership:
        if ownership.file is None:
            print(json.dumps({'state':'observer_busy','network_requested':False})); return 2
        atomic_json(LOCK_PATH.with_suffix('.owner.json'),{'session_directory':str(directory.resolve()),'kind':'finite'})
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=15,
                          limits=httpx.Limits(max_keepalive_connections=0),
                          headers={'User-Agent':USER_AGENT}) as client:
            observer = ECBObserver(client, directory, options, stop_file=args.stop_file)
            observer.last_host=observer.start
            # Python handles Ctrl+C; SIGTERM also finalises evidence where supported.
            previous = signal.getsignal(signal.SIGTERM)
            def interrupt(*_):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, interrupt)
            try:
                result = observer.run()
            finally:
                signal.signal(signal.SIGTERM, previous)
    print(json.dumps({k:result[k] for k in ('state','stop_reason','completed_rounds','requests',
                      'new_observed_candidates','paired_new_candidates','sample_conclusion')}, ensure_ascii=False, indent=2))
    return 0 if result['state'] == 'completed' else 130 if result['state'] == 'interrupted' else 2


if __name__ == '__main__':
    raise SystemExit(main())
