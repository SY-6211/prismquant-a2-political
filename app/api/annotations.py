"""Admin corpus management and separately authenticated human reviewer entrypoints."""
import json
import secrets
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import AwareDatetime,Field,model_validator
from sqlalchemy import text
from app.annotation_model import Strict,Label
from app.api.keywords import admin,bearer,execute as keyword_execute
from app.repositories import annotations as repo

def no_store(response:Response):response.headers['Cache-Control']='no-store'


router=APIRouter(prefix='/api/v1/annotation-datasets',tags=['标注数据集管理'],dependencies=[Depends(admin),Depends(no_store)])
review_router=APIRouter(prefix='/api/v1/annotation-review',tags=['人工标注'],dependencies=[Depends(no_store)])


def execute(request,fn,*args):
    def checked(conn):
        try:return fn(conn,*args)
        except (repo.Conflict,repo.Missing):raise
        except ValueError as error:raise HTTPException(422,detail={'code':'annotation_invalid','message':str(error)}) from None
    return keyword_execute(request,checked)


def reviewer(request:Request,credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
    raw=request.app.state.settings.annotation_reviewer_tokens.get_secret_value()
    if not raw:raise HTTPException(503,detail={'code':'reviewers_disabled','message':'尚未配置独立标注员凭证。'})
    token=credentials.credentials if credentials else ''
    identity=None
    for name,value in json.loads(raw).items():
        if secrets.compare_digest(value.encode(),token.encode()):identity=name
    if identity is None:raise HTTPException(401,detail={'code':'reviewer_required','message':'需要独立标注员权限。'},headers={'WWW-Authenticate':'Bearer'})
    return identity


class Create(Strict):name:str=Field(min_length=1,max_length=120)


class Sample(Strict):
    record_id:UUID|None=None
    version_id:UUID|None=None
    body:str|None=Field(default=None,min_length=1,max_length=100000)
    person_id:UUID|None=None
    published_at:AwareDatetime|None=None
    group_hint:str|None=Field(default=None,min_length=1,max_length=120)

    @model_validator(mode='after')
    def choose_one(self):
        if sum(v is not None for v in (self.record_id,self.version_id,self.body))!=1:raise ValueError('Choose exactly one record, version, or synthetic body')
        return self


class Capture(Strict):
    limit:int=Field(default=200,ge=1,le=500)
    after_record_id:UUID|None=None


class Assessment(Strict):
    label:Label
    expected_revision:int=Field(default=0,ge=0)
    adjudicate:bool=False


class Benchmark(Strict):
    split:Literal['train','dev']='dev'
    version_ids:list[UUID]=Field(min_length=1,max_length=20)


def page(rows,limit):return {'items':rows[:limit],'next_cursor':rows[limit-1]['sample_id'] if len(rows)>limit else None}


def capture_records(conn,identity,limit,after_record_id=None):
    ids=list(conn.execute(text('SELECT record_id FROM public_records WHERE length(body_text)<=100000 '
        'AND (CAST(:after AS uuid) IS NULL OR record_id>:after) ORDER BY record_id LIMIT :n'),{'n':limit+1,'after':after_record_id}).scalars())
    values=[repo.add(conn,identity,record_id=rid) for rid in ids[:limit]]
    return {'selected':len(values),'created':sum(v['created'] for v in values),'capture_limit':limit,
        'next_record_id':ids[limit-1] if len(ids)>limit else None,'selection':'record_id order, main text only'}


@router.get('')
def listing(request:Request,limit:int=Query(default=20,ge=1,le=100),cursor:UUID|None=None):
    rows=execute(request,repo.datasets,limit,cursor)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['dataset_id'] if len(rows)>limit else None}


@router.post('')
def create(request:Request,body:Create):return execute(request,repo.create,body.name)


@router.get('/{dataset_id}')
def detail(request:Request,dataset_id:UUID):return execute(request,repo.dataset,dataset_id)


@router.post('/{dataset_id}/samples')
def add(request:Request,dataset_id:UUID,body:Sample):
    def save(conn):return repo.add(conn,dataset_id,**body.model_dump())
    return execute(request,save)


@router.post('/{dataset_id}/capture-records')
def capture(request:Request,dataset_id:UUID,body:Capture):return execute(request,capture_records,dataset_id,body.limit,body.after_record_id)


@router.post('/{dataset_id}/seal')
def seal(request:Request,dataset_id:UUID):return execute(request,repo.seal,dataset_id)


@router.get('/{dataset_id}/samples')
def samples(request:Request,dataset_id:UUID,split:Literal['train','dev']='train',limit:int=Query(default=20,ge=1,le=100),cursor:UUID|None=None):
    return page(execute(request,repo.samples,dataset_id,split,limit,cursor),limit)


@router.get('/{dataset_id}/export')
def export(request:Request,dataset_id:UUID,split:Literal['train','dev']='train'):
    rows=execute(request,repo.export_rows,dataset_id,split)
    return Response(''.join(json.dumps(row,ensure_ascii=False,default=str)+'\n' for row in rows),media_type='application/x-ndjson',headers={'Cache-Control':'no-store'})


@router.post('/{dataset_id}/benchmark')
def benchmark(request:Request,dataset_id:UUID,body:Benchmark):return execute(request,repo.benchmark,dataset_id,body.split,body.version_ids)


@router.get('/{dataset_id}/runs/{run_id}')
def report(request:Request,dataset_id:UUID,run_id:UUID):return execute(request,repo.run_report,dataset_id,run_id)


@review_router.get('/{dataset_id}/samples')
def review_samples(request:Request,dataset_id:UUID,split:Literal['train','dev','blind']='train',limit:int=Query(default=20,ge=1,le=100),cursor:UUID|None=None,actor:str=Depends(reviewer)):
    def read(conn):return repo.samples(conn,dataset_id,split,limit,cursor,reviewer=True)
    return page(execute(request,read),limit)


@review_router.post('/{dataset_id}/samples/{sample_id}/labels')
def label(request:Request,dataset_id:UUID,sample_id:UUID,body:Assessment,actor:str=Depends(reviewer)):
    def save(conn):return repo.annotate(conn,dataset_id,sample_id,actor,body.label,body.expected_revision,adjudicate=body.adjudicate)
    return execute(request,save)


@review_router.get('/{dataset_id}/samples/{sample_id}/labels')
def labels(request:Request,dataset_id:UUID,sample_id:UUID,actor:str=Depends(reviewer)):
    return {'items':execute(request,repo.own_labels,dataset_id,sample_id,actor)}


@review_router.get('/{dataset_id}/disputes')
def disputes(request:Request,dataset_id:UUID,split:Literal['train','dev','blind']='dev',actor:str=Depends(reviewer)):
    return execute(request,repo.disputes,dataset_id,split)
