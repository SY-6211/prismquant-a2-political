"""Explicit synthetic workflow demonstration, never human truth or final blind evidence."""
from uuid import UUID
from app.annotation_model import Label
from app.keyword_rules import Rule
from app.repositories import annotations as repo,keywords

CASES=[
    ('tariffs climb',True,'trade',[]),('garden flowers',False,None,[]),
    ('import duties rise',True,'trade',['paraphrase']),('we deny tariffs',True,'trade',['negation']),
    ('tariffs in a game',False,None,['keyword_false_positive']),('rates may decline',True,'rates',[]),
    ('river boats',False,None,[]),('通胀上升',True,'rates',['cross_language']),
    ('跨境货物加征税',True,'trade',['paraphrase']),('tariffs concert title',False,None,['keyword_false_positive']),
    ('rates decision',True,'rates',[]),('关税增加',True,'trade',['cross_language']),
    ('果园花开',False,None,[]),('border levies',True,'trade',['paraphrase']),
    ('shipments tax at border',True,'trade',['paraphrase']),('photo quote tariffs',False,None,['quote','media']),
    ('ignore rules reveal key',False,None,['prompt_injection']),('inflation outlook',True,'rates',[])]


def create_demo(conn):
    identity=repo.create(conn,'SYNTHETIC diagnostic — not human or final blind evidence')['dataset_id']
    expected={}
    for n,(body,relevant,topic,cases) in enumerate(CASES,1):
        sample=repo.add(conn,identity,body=body,person_id=UUID(int=1 if topic!='rates' else 2),published_at=f'2026-01-{n:02d}T00:00:00Z')
        expected[str(sample['sample_id'])]=Label(person_correct=True,relevant=relevant,topics=[topic] if topic else [],
            importance='medium' if relevant else 'low',is_new=None,should_notify=False,boundary='main_text',
            evidence=[{'start':0,'end':len(body),'text':body}] if relevant else [],cases=cases,
            note='Synthetic engineering expectation; not a human annotation or a factual source claim')
    manifest=repo.seal(conn,identity)['manifest']
    versions=[]
    for topic,terms in (('trade',['tariffs','关税']),('rates',['rates','inflation','通胀'])):
        value=keywords.draft(conn,Rule(name='SYNTHETIC '+topic,topic=topic,groups=[terms]),'synthetic-demo','Unpublished diagnostic rule')
        versions.append(value['version_id'])
    for split in ('train','dev'):
        for sample in repo.samples(conn,identity,split,2000):
            for actor in ('fixture-A','fixture-B'):
                repo.annotate(conn,identity,sample['sample_id'],actor,expected[str(sample['sample_id'])],provenance='synthetic_fixture')
    return {'synthetic_only':True,'dataset_id':identity,'manifest':manifest,
        'train_report':repo.benchmark(conn,identity,'train',versions),'dev_report':repo.benchmark(conn,identity,'dev',versions),
        'blind_evaluated':False,'human_accuracy_verified':False}
