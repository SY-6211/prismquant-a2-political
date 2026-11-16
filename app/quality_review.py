"""Human review helper. No model, invented label, embedded credential or remote upload."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
import httpx
from app.annotation_model import digest
from app.quality_model import Review


def read_json(path):
    if path.stat().st_size > 2_000_000: raise ValueError('File too large')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def template(value):
    packet=value['packet']
    if digest(packet)!=value['packet_hash']: raise ValueError('Snapshot hash differs')
    own=value.get('own_review')
    return {'expected_revision':own['revision'] if own else 0,'independent_human_review':None,
        'relevance':[{'version_id':p['version_id'],'relevant':None,'evidence':[]} for p in packet['pool']],
        'supported':None,'person_correct':None,'time_correct':None,'useful':None,'note':''}


def local_base(value):
    url=urlsplit(value)
    if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1') or url.path not in ('','/') or url.query or url.fragment or url.username or url.password:
        raise ValueError('Review helper only connects to an explicit local API origin')
    return value.rstrip('/')


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    t=sub.add_parser('template');t.add_argument('--packet-file',type=Path,required=True);t.add_argument('--out',type=Path,required=True)
    for command in ('get','submit'):
        q=sub.add_parser(command);q.add_argument('--case-id',type=UUID,required=True)
        q.add_argument('--reviewer-token-file',type=Path,required=True);q.add_argument('--base-url',default='http://127.0.0.1:8000',type=local_base)
        q.add_argument('--out',type=Path,required=True)
        if command=='submit':q.add_argument('--label-file',type=Path,required=True)
    args=p.parse_args()
    try:
        if args.out.exists(): raise ValueError('Output already exists; preserve review history')
        if args.command=='template':result=template(read_json(args.packet_file))
        else:
            if args.reviewer_token_file.stat().st_size>1024:raise ValueError('Invalid token file')
            token=args.reviewer_token_file.read_text(encoding='utf-8-sig').strip()
            if not 32<=len(token)<=256 or any(ord(c)<=32 for c in token):raise ValueError('Invalid reviewer credential')
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=15) as client:
                path=args.base_url+'/api/v1/quality-review/'+str(args.case_id)
                headers={'Authorization':'Bearer '+token,'Accept':'application/json'}
                if args.command=='get':response=client.get(path,headers=headers)
                else:
                    label=Review.model_validate(read_json(args.label_file))
                    response=client.post(path,headers=headers,json=label.model_dump(mode='json'))
                if response.status_code!=200:
                    print(json.dumps({'status':'failed','http_status':response.status_code,'message':'检查独立标注员凭证、修订号、完整候选判断和原文偏移；不自动重试。'},ensure_ascii=False));return 1
                result=response.json()
        with args.out.open('x',encoding='utf8') as file:json.dump(result,file,ensure_ascii=False,indent=2,default=str)
        print(json.dumps({'status':'saved','path':str(args.out),'model_requests':0},ensure_ascii=False));return 0
    except (ValueError,OSError,httpx.HTTPError):
        print(json.dumps({'status':'failed','message':'检查本机地址、文件、快照哈希及人工填写项；空模板不能作为标注提交。'},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
