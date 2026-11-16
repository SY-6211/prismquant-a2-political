"""Bounded OFFLINE Truth adapter replay and access checks. No live connect command."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter

from app.core.config import PROJECT_ROOT
from app.ecb_observation_support import ObserverLock, atomic_json, read_journal, read_json
from app.truth_fast_access import AccessManifest, DataCredentials, readiness
from app.truth_fast_contract import ADAPTER_VERSION, MAX_FRAME_BYTES, NormalizedEvent, normalize_frame

MAX_INPUT = 8 * 1024 * 1024
MAX_LINES = 1000


def unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON member')
        result[key] = value
    return result


def strict_json(value):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON value')
    return json.loads(value,object_pairs_hook=unique_members,parse_constant=invalid_constant)


class ExportRow(BaseModel):
    model_config = ConfigDict(extra='forbid',hide_input_in_errors=True)
    kind: str = 'post'
    received_at: AwareDatetime
    frame: dict | None = None
    gap_started_at: AwareDatetime | None = None
    gap_ended_at: AwareDatetime | None = None


def decode_line(line, expected_user_id):
    if len(line) > MAX_FRAME_BYTES:
        return {'code':'frame_too_large'}
    try:
        row = ExportRow.model_validate(strict_json(line))
        if row.kind == 'gap':
            if row.frame is not None or row.gap_started_at is None or row.gap_ended_at is None or row.gap_ended_at < row.gap_started_at:
                return {'code':'invalid_gap'}
            return {'code':'gap','gap_started_at':row.gap_started_at.isoformat(),'gap_ended_at':row.gap_ended_at.isoformat(),
                    'coverage_restored':False,'automatic_backfill_available':False}
        if row.kind != 'post' or row.frame is None:
            return {'code':'invalid_export'}
        if row.frame.get('platform') != 'truth':
            return {'code':'unsupported_platform'}
        # Check target identity before parsing or retaining somebody else's text.
        if row.frame.get('user_id') != expected_user_id or str(row.frame.get('username','')).casefold() != 'realdonaldtrump':
            return {'code':'identity_mismatch'}
        event = normalize_frame(row.frame,expected_user_id,row.received_at)
        return {'code':'normalized','event':event.model_dump(mode='json'),'content_hash':event.content_hash()}
    except (ValueError, TypeError, RecursionError):
        # Do not serialize validation exceptions: they may include secrets or raw frames.
        return {'code':'schema_error'}


def replay(input_path, output_root, expected_user_id, *, max_frames=50, resume=None, synthetic=False):
    import re
    if not re.fullmatch(r'[1-9][0-9]{0,29}',expected_user_id) or type(max_frames) is not int or not 1 <= max_frames <= 1000:
        raise ValueError('Invalid identity or finite replay bound')
    input_path = Path(input_path)
    with input_path.open('rb') as stream:
        data = stream.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('Input export exceeds 8 MiB')
    lines = data.splitlines()
    if len(lines) > MAX_LINES:
        raise ValueError('Input export exceeds 1000 lines')
    manifest = {'adapter_version':ADAPTER_VERSION,'input_sha256':hashlib.sha256(data).hexdigest(),
                'expected_user_id':expected_user_id,'synthetic_input_declared':synthetic}
    directory = Path(resume) if resume else Path(output_root)/('replay-'+uuid4().hex[:12])
    if resume and not directory.is_dir():
        raise ValueError('Replay to resume does not exist')
    if not resume:
        directory.mkdir(parents=True,exist_ok=False)
    with ObserverLock(directory/'replay.lock') as lock:
        if lock.file is None:
            raise ValueError('Replay is already running')
        if resume:
            if read_json(directory/'manifest.json') != manifest:
                raise ValueError('Input, account pin, synthetic label or adapter version changed')
        else:
            atomic_json(directory/'manifest.json',manifest)
        journal = directory/'observations.jsonl'
        previous, partial = read_journal(journal)
        versions, last_hash, counts = {}, {}, Counter()
        def apply(record):
            if record.get('line_number') != sum(counts.values())+1:
                raise ValueError('Replay journal order is corrupt')
            event = record.get('event')
            if event:
                if NormalizedEvent.model_validate(event).content_hash() != record['content_hash']:
                    raise ValueError('Replay content digest mismatch')
                key, digest = event['event_id'], record['content_hash']
                versions.setdefault(key,{})
                if digest not in versions[key]:
                    versions[key][digest] = len(versions[key])+1
                last_hash[key] = digest
            counts[record['code']] += 1
        for record in previous:
            apply(record)
        if partial:
            # Recover only an incomplete final line; interior corruption fails above.
            repair = journal.with_suffix('.repair')
            repair.write_text(''.join(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n' for r in previous),encoding='utf-8')
            repair.replace(journal)
        start = len(previous)
        if start > len(lines):
            raise ValueError('Journal exceeds declared input')
        for index in range(start,min(len(lines),start+max_frames)):
            record = {'line_number':index+1,**decode_line(lines[index],expected_user_id)}
            event = record.get('event')
            if event:
                key, digest = event['event_id'], record['content_hash']
                known = versions.get(key,{})
                record['code'] = ('duplicate' if last_hash.get(key)==digest else 'version_returned') if digest in known else ('revision' if known else 'normalized')
                record['version_number'] = known.get(digest,len(known)+1)
            with journal.open('a',encoding='utf-8',newline='\n') as stream:
                stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            apply(record)
            atomic_json(directory/'checkpoint.json',{'next_line':index+2,'input_sha256':manifest['input_sha256']})
        summary = {'mode':'offline_truth_replay','adapter_version':ADAPTER_VERSION,'directory':str(directory.resolve()),
            'synthetic_input_declared':synthetic,'input_lines':len(lines),'processed_total':sum(counts.values()),
            'processed_this_segment':sum(counts.values())-start,'remaining':len(lines)-sum(counts.values()),
            'status':'completed' if sum(counts.values())==len(lines) else 'partial','counts':dict(counts),
            'events':len(versions),'content_versions':sum(len(v) for v in versions.values()),
            'recovered_partial_journal':partial,'coverage_complete':False,'publication_latency_verified':False,
            'database_saved':False,'network_requested':False,'model_called':False,'live_ingestion_ready':False}
        atomic_json(directory/'summary.json',summary)
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    check = sub.add_parser('check',help='只读检查接入配置完整度，不验证账号、不联网')
    check.add_argument('--manifest',type=Path)
    off = sub.add_parser('replay',help='只处理本地导出/合成 JSONL，不连接数据商')
    off.add_argument('--input',type=Path,required=True)
    off.add_argument('--expected-user-id',required=True,help='已声明的固定数字账号 ID；样例 ID 不代表真实账号')
    off.add_argument('--output',type=Path,default=PROJECT_ROOT/'.runtime/truth-fast')
    off.add_argument('--resume',type=Path)
    off.add_argument('--max-frames',type=int,default=50)
    off.add_argument('--synthetic',action='store_true',help='明确标注合成数据；恢复时须保留相同标记')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            config = AccessManifest.model_validate(read_json(args.manifest,limit=32768)) if args.manifest else AccessManifest()
            result = readiness(config,DataCredentials())
        else:
            result = replay(args.input,args.output,args.expected_user_id,max_frames=args.max_frames,resume=args.resume,synthetic=args.synthetic)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError):
        print(json.dumps({'status':'stopped','code':'offline_input_or_configuration_invalid',
                          'message':'离线文件、账号固定值、恢复目录或配置需检查；未显示原始消息和凭证。',
                          'network_requested':False,'database_saved':False},ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
