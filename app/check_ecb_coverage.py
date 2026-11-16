"""Offline comparison with an explicit, manually reviewed reference list.

Calendar activities without a promised text are excluded; an empty target list
never becomes 100% coverage. Reference URLs are recorded, never fetched here.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from app.ecb_observation_support import atomic_json,read_json,read_journal
from app.observe_ecb_session import load_state,timestamp
from app.preview_ecb_speech import speech_url


def check_manifest(value):
    if value.get('schema_version')!=1:raise ValueError('Unknown reference schema')
    since,until=timestamp(value['window']['since']),timestamp(value['window']['until'])
    if since>=until:raise ValueError('Invalid reference window')
    entries=value['entries']
    if not isinstance(entries,list) or len(entries)>500:raise ValueError('Reference list exceeds limit')
    seen=set()
    for item in entries:
        if item.get('review_status') not in ('confirmed_text','scheduled_no_text','pending'):
            raise ValueError('Unknown reference review status')
        reference=urlsplit(item['reference_url'])
        if (reference.scheme!='https' or reference.netloc!='www.ecb.europa.eu'
                or reference.query or reference.fragment
                or not reference.path.startswith(('/press/calendars/','/press/key/','/press/pubbydate/'))):
            raise ValueError('Not an ECB reference page')
        if item['review_status']=='confirmed_text':
            url=speech_url(item['publication_url'])
            if url in seen:raise ValueError('Duplicate reference identity')
            seen.add(url)
            stamp=timestamp(item['reference_seen_at'])
            if not since<=stamp<=until:raise ValueError('Reference time is outside its declared window')
            if not item.get('speaker_confirmed'):raise ValueError('Text attribution has not been confirmed')
    return entries


def coverage(session,manifest):
    state,_=load_state(session); reference=read_json(manifest,1024*1024)
    entries=check_manifest(reference); rows=[]; excluded=[]
    since,until=timestamp(reference['window']['since']),timestamp(reference['window']['until'])
    observed={'rss':set(),'index':set()}; views=0
    for segment in state['segments']:
        snapshots,_=read_journal(Path(session)/'segments'/segment['segment_id']/'snapshots.jsonl')
        for snapshot in snapshots:
            if snapshot.get('channel') not in observed or 'checked_at' not in snapshot:continue
            if since<=timestamp(snapshot['checked_at'])<=until:
                views+=1
                observed[snapshot['channel']].update(snapshot.get('candidate_urls',[]))
    for item in entries:
        if item['review_status']!='confirmed_text':
            excluded.append({'label':item.get('label'),'reason':item['review_status'],
                             'reference_url':item['reference_url']}); continue
        url=speech_url(item['publication_url']); event=state['candidates'].get(url)
        seen={channel for channel,urls in observed.items() if url in urls}
        rows.append({'url':url,'label':item.get('label'),'reference_url':item['reference_url'],
                     'rss_observed':'rss' in seen,'index_observed':'index' in seen,
                     'baseline_or_gap_catchup':bool(event and event['baseline_channels']),
                     'state':'observed_in_returned_window' if seen else 'not_observed_in_checked_windows' if views else 'outside_observed_window',
                     'publication_latency':'not_measured'})
    matched=sum(row['rss_observed'] or row['index_observed'] for row in rows)
    return {'mode':'offline_ecb_selected_reference_check','session_id':state['session_id'],
            'reference_window':reference['window'],'expected_text_targets':len(rows),
            'observed_selected_targets':matched,'excluded_or_pending_entries':excluded,
            'selected_reference_match_ratio':matched/len(rows) if rows and views else None,
            'state':'no_eligible_text_targets' if not rows else 'selected_reference_comparison_only' if views else 'no_observation_overlap',
            'observed_views_in_reference_window':views,
            'rows':rows,'independent_completeness':'not_verified','public_to_browser':'not_measured',
            'network_requested':False,'database_accessed':False,
            'notes':['参照清单是明确列出的人工核对输入，不代表完整发布集合。',
                     '未在有限窗口见到不等于来源漏报；无可用目标时不计算百分比。',
                     '活动时间或日历更新时间不替代讲话原文首次公开时刻。']}


def main(argv=None):
    parser=argparse.ArgumentParser(description='离线核对已观察记录与人工参照清单')
    parser.add_argument('--session',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.output.exists():parser.error('Choose a new output file; old evidence is preserved')
    try:result=coverage(args.session,args.manifest)
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'state':'invalid_reference_or_session','error_type':type(error).__name__})); return 2
    args.output.parent.mkdir(parents=True,exist_ok=True); atomic_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


if __name__=='__main__':raise SystemExit(main())
