"""Explicit corpus lifecycle. Blind samples never flow through developer exports/evals."""
import json
import hashlib
from datetime import datetime,timezone
from uuid import UUID,uuid4
from sqlalchemy import text
from app.annotation_model import Label,digest,grouped_splits,summarize,METRIC_VERSION,canonical
from app.keyword_rules import Rule,evaluate,ENGINE_VERSION
from app.repositories.keywords import Conflict,Missing


def dataset(conn,identity,lock=False):
    row=conn.execute(text('SELECT * FROM annotation_datasets WHERE dataset_id=:id'+(' FOR UPDATE' if lock else '')),{'id':identity}).mappings().one_or_none()
    if row is None:raise Missing('Dataset not found')
    return dict(row)


def create(conn,name):
    if not name.strip() or len(name)>120:raise ValueError('Dataset name needs 1..120 characters')
    identity=uuid4()
    conn.execute(text('INSERT INTO annotation_datasets(dataset_id,name) VALUES (:id,:name)'),{'id':identity,'name':name.strip()})
    return dataset(conn,identity)


def datasets(conn,limit=20,after=None):
    return [dict(row) for row in conn.execute(text('SELECT * FROM annotation_datasets WHERE '
        '(CAST(:after AS uuid) IS NULL OR dataset_id>:after) ORDER BY dataset_id LIMIT :n'),{'after':after,'n':limit+1}).mappings()]


def add(conn,identity,*,record_id=None,version_id=None,body=None,person_id=None,published_at=None,group_hint=None):
    current=dataset(conn,identity,True)
    if current['state']!='draft':raise Conflict('Corpus is sealed; create another dataset')
    if sum(v is not None for v in (record_id,version_id,body))!=1:raise ValueError('Choose record, version, or explicit synthetic body')
    if group_hint is not None and (not group_hint.strip() or len(group_hint)>120):raise ValueError('Invalid manual lineage group')
    origin='source';event_key=None
    if record_id is not None:
        row=conn.execute(text('SELECT r.*,l.event_id,l.version_id FROM public_records r LEFT JOIN event_record_links l USING(record_id) '
            'WHERE r.record_id=:id'),{'id':record_id}).mappings().one_or_none()
        if row is None:raise Missing('Record not found')
        body=row['body_text'];person_id=row['person_id'];published_at=row['published_at'];event_key=str(row['event_id']) if row['event_id'] else row['original_url'] or row['retrieved_url']
        source={'record_id':str(record_id),'event_version_id':str(row['version_id']) if row['version_id'] else None,
            'retrieved_url':row['retrieved_url'],'verification_status':row['verification_status'],'provenance_type':row['provenance_type']}
    elif version_id is not None:
        row=conn.execute(text('SELECT event_id,payload FROM event_versions WHERE version_id=:id'),{'id':version_id}).mappings().one_or_none()
        if row is None:raise Missing('Event version not found')
        payload=row['payload'];body=payload['main_text'];person_id=payload['person_id'];published_at=payload['published_at'];event_key=str(row['event_id'])
        source={'event_version_id':str(version_id),'retrieved_url':payload['retrieved_url'],
            'verification_status':payload['verification_status'],'provenance_type':payload['provenance_type']}
    else:origin='synthetic';source={'provenance_type':'synthetic_fixture','verification_status':'not_source_evidence'}
    if not body or len(body)>100000 or not canonical(body):raise ValueError('Main text must have 1..100,000 characters; media-only events need another task')
    if published_at:
        if isinstance(published_at,str):published_at=datetime.fromisoformat(published_at.replace('Z','+00:00'))
        if published_at.utcoffset() is None:raise ValueError('Publication time requires timezone')
        published_at=published_at.astimezone(timezone.utc).isoformat()
    snapshot={**source,'body':body,'body_sha256':hashlib.sha256(body.encode()).hexdigest(),'person_id':str(person_id) if person_id else None,
        'published_at':published_at,'captured_at':datetime.now(timezone.utc).isoformat()}
    sample_identity=digest({'record':str(record_id) if record_id else None,'version':str(version_id) if version_id else None,
        'body':body,'person':str(person_id),'origin':origin})
    existing=conn.execute(text('SELECT sample_id,group_hint FROM annotation_samples WHERE dataset_id=:d AND identity=:i'),{'d':identity,'i':sample_identity}).mappings().one_or_none()
    if existing:
        if existing['group_hint']!=group_hint:raise Conflict('Duplicate sample has another lineage hint; create a corrected draft corpus')
        return {'sample_id':existing['sample_id'],'created':False}
    count,characters=conn.execute(text("SELECT count(*),coalesce(sum(length(snapshot->>'body')),0) FROM annotation_samples WHERE dataset_id=:id"),{'id':identity}).one()
    if count>=2000 or characters+len(body)>5000000:raise Conflict('Corpus capacity exceeded')
    sid=uuid4()
    conn.execute(text('INSERT INTO annotation_samples(sample_id,dataset_id,identity,origin,snapshot,event_key,group_hint) '
        'VALUES (:s,:d,:i,:o,CAST(:v AS jsonb),:event,:hint)'),{'s':sid,'d':identity,'i':sample_identity,'o':origin,
        'v':json.dumps(snapshot,ensure_ascii=False),'event':event_key,'hint':group_hint})
    return {'sample_id':sid,'created':True}


def seal(conn,identity):
    current=dataset(conn,identity,True)
    if current['state']!='draft':raise Conflict('Already sealed')
    samples=[dict(row) for row in conn.execute(text('SELECT * FROM annotation_samples WHERE dataset_id=:id ORDER BY sample_id'),{'id':identity}).mappings()]
    assignment,manifest=grouped_splits(samples)
    for item in assignment:conn.execute(text('UPDATE annotation_samples SET group_id=:g,split=:split WHERE sample_id=:s'),
        {'g':item['group_id'],'split':item['split'],'s':item['sample_id']})
    conn.execute(text("UPDATE annotation_datasets SET state='sealed',manifest=CAST(:m AS jsonb) WHERE dataset_id=:id"),{'id':identity,'m':json.dumps(manifest)})
    return dataset(conn,identity)


def samples(conn,identity,split='train',limit=20,after=None,*,reviewer=False):
    current=dataset(conn,identity)
    if not reviewer and (current['state']!='sealed' or split=='blind'):raise Conflict('Developer access requires sealed train/dev; blind is reserved for independent evaluation')
    if split not in ('train','dev','blind'):raise ValueError('Invalid split')
    return [dict(row) for row in conn.execute(text('SELECT sample_id,origin,snapshot,group_id,split FROM annotation_samples '
        'WHERE dataset_id=:d AND (split=:split OR split IS NULL) AND (CAST(:after AS uuid) IS NULL OR sample_id>:after) '
        'ORDER BY sample_id LIMIT :n'),{'d':identity,'split':split,'after':after,'n':limit+1}).mappings()]


def latest_labels(conn,sample_id):
    return [dict(r) for r in conn.execute(text("SELECT DISTINCT ON(reviewer) * FROM annotation_labels WHERE sample_id=:s AND kind='label' "
        'ORDER BY reviewer,revision DESC'),{'s':sample_id}).mappings()]


def basis(rows):return digest(sorted(str(r['label_id']) for r in rows))


def resolved(conn,sample_id):
    rows=latest_labels(conn,sample_id)
    if not rows:return {'status':'unlabelled','label':None,'label_ids':[],'human':False}
    fingerprints={digest(Label.model_validate(r['label']).judgment()) for r in rows}
    human=all(r['provenance']=='human_verified' for r in rows)
    base={'label_ids':[str(r['label_id']) for r in rows],'human':human,'reviewers':len(rows)}
    if len(fingerprints)>1:
        adjudication=conn.execute(text("SELECT * FROM annotation_labels WHERE sample_id=:s AND kind='adjudication' AND basis_digest=:b "
            'ORDER BY created_at DESC,label_id DESC LIMIT 1'),{'s':sample_id,'b':basis(rows)}).mappings().one_or_none()
        if not adjudication:return {**base,'status':'disputed','label':None}
        label=adjudication['label'];base['label_ids'].append(str(adjudication['label_id']));base['human']=human and adjudication['provenance']=='human_verified'
        status='adjudicated'
    else:label=rows[0]['label'];status='consensus' if len(rows)>=2 else 'provisional'
    if label['needs_review'] or label['relevant'] is None or label['boundary']=='uncertain':status='uncertain'
    return {**base,'status':status,'label':label}


def annotate(conn,identity,sample_id,reviewer,label,expected_revision=0,*,adjudicate=False,provenance='human_verified'):
    current=dataset(conn,identity)
    if current['state']!='sealed':raise Conflict('Seal the corpus before annotation')
    sample=conn.execute(text('SELECT * FROM annotation_samples WHERE dataset_id=:d AND sample_id=:s FOR UPDATE'),{'d':identity,'s':sample_id}).mappings().one_or_none()
    if sample is None:raise Missing('Sample not in dataset')
    if provenance not in ('human_verified','synthetic_fixture') or (provenance=='synthetic_fixture' and sample['origin']!='synthetic'):raise ValueError('Synthetic labels cannot label real source evidence')
    label=Label.model_validate(label);label.validate_evidence(sample['snapshot']['body'])
    revision=conn.execute(text('SELECT coalesce(max(revision),0) FROM annotation_labels WHERE sample_id=:s AND reviewer=:r'),{'s':sample_id,'r':reviewer}).scalar_one()
    if revision!=expected_revision:raise Conflict('Label changed; load your current revision')
    rows=latest_labels(conn,sample_id);base=None
    if adjudicate:
        if len(rows)<2 or len({digest(Label.model_validate(r['label']).judgment()) for r in rows})<2:raise Conflict('Adjudication needs an actual disagreement')
        if reviewer in {r['reviewer'] for r in rows}:raise Conflict('An independent third reviewer must adjudicate')
        if label.needs_review or label.relevant is None:raise ValueError('Adjudication must resolve uncertainty')
        base=basis(rows)
    lid=uuid4()
    conn.execute(text('INSERT INTO annotation_labels(label_id,sample_id,reviewer,revision,kind,provenance,label,basis_digest) '
        'VALUES (:id,:s,:r,:n,:k,:p,CAST(:v AS jsonb),:b)'),{'id':lid,'s':sample_id,'r':reviewer,'n':revision+1,
        'k':'adjudication' if adjudicate else 'label','p':provenance,'v':label.model_dump_json(),'b':base})
    return {'label_id':lid,'revision':revision+1,'sample_id':sample_id}


def own_labels(conn,identity,sample_id,reviewer):
    if not conn.execute(text('SELECT 1 FROM annotation_samples WHERE sample_id=:s AND dataset_id=:d'),{'s':sample_id,'d':identity}).scalar_one_or_none():raise Missing('Sample not in dataset')
    return [dict(r) for r in conn.execute(text('SELECT * FROM annotation_labels WHERE sample_id=:s AND reviewer=:r ORDER BY revision DESC LIMIT 100'),{'s':sample_id,'r':reviewer}).mappings()]


def disputes(conn,identity,split='dev'):
    # Reviewer-only endpoint; developers cannot use this to inspect blind labels.
    rows=samples(conn,identity,split,2000,reviewer=True)
    result=[]
    for row in rows:
        value=resolved(conn,row['sample_id'])
        if value['status']=='disputed':result.append({'sample_id':row['sample_id'],'labels':latest_labels(conn,row['sample_id'])})
    return {'items':result}


def export_rows(conn,identity,split):
    rows=samples(conn,identity,split,2000)
    output=[]
    for row in rows:
        truth=resolved(conn,row['sample_id'])
        output.append({**row,'annotation':truth})
    return output


def benchmark(conn,identity,split,version_ids):
    if not 1<=len(version_ids)<=20 or len(set(version_ids))!=len(version_ids):raise ValueError('Select 1..20 unique explicit rule versions')
    data=dataset(conn,identity);rows=export_rows(conn,identity,split)
    rules=[]
    for vid in version_ids:
        row=conn.execute(text('SELECT * FROM keyword_versions WHERE version_id=:v'),{'v':vid}).mappings().one_or_none()
        if row is None:raise Missing('Rule version not found')
        rules.append({'version_id':str(vid),'digest':row['digest'],'rule':Rule.model_validate(row['definition'])})
    if sum(len(row['snapshot']['body']) for row in rows)*len(rules)>10000000:raise ValueError('Evaluation exceeds 10,000,000 rule/text characters')
    eligible=[];excluded={};cases=[];human=[];fixture=[];review_predictions=0
    topics={r['rule'].topic for r in rules}
    for row in rows:
        truth=row['annotation']
        if truth['status'] not in ('consensus','adjudicated'):
            excluded[truth['status']]=excluded.get(truth['status'],0)+1;continue
        label=truth['label'];topics.update(label['topics']);snapshot=row['snapshot'];predicted=set();unassessed=False;predictions=[]
        for rule in rules:
            value=evaluate(rule['rule'],snapshot['body'],snapshot['person_id']);predictions.append({'version_id':rule['version_id'],**value})
            if value['status'] in ('matched','needs_review'):predicted.add(rule['rule'].topic)
            if value['status']=='needs_review':review_predictions+=1
            if value['status']=='unassessed':unassessed=True
        item={'label':label,'predicted_topics':sorted(predicted),'person_id':snapshot['person_id'],'unassessed':unassessed}
        eligible.append(item)
        (human if truth['human'] and row['origin']=='source' else fixture).append(item)
        cases.append({'sample_id':str(row['sample_id']),'body_sha256':snapshot['body_sha256'],'group_id':row['group_id'],
            'label_ids':truth['label_ids'],'truth_status':truth['status'],'source_human_reviewed':truth['human'] and row['origin']=='source',
            'expected_topics':label['topics'],'predictions':predictions,'case_tags':label['cases']})
    report={'metric_version':METRIC_VERSION,'keyword_engine_version':ENGINE_VERSION,'dataset_id':str(identity),'split':split,
        'manifest_sha256':data['manifest']['manifest_sha256'],'rules':[{'version_id':r['version_id'],'digest':r['digest']} for r in rules],
        'sample_count':len(rows),'reviewed_eligible':len(eligible),'excluded':excluded,'reviewed_fraction':len(eligible)/len(rows) if rows else 0,
        'human_source_count':len(human),'synthetic_or_fixture_count':len(fixture),'needs_review_predictions':review_predictions,
        'human_source_metrics':summarize(human,sorted(topics)),'synthetic_diagnostic_metrics':summarize(fixture,sorted(topics)),
        'cases':cases,'final_agent_quality_verified':False,'sufficient_for_final_acceptance':False,
        'limitations':['Only agreed/adjudicated labels scored; provisional/uncertain/disputed/unlabelled excluded.',
            'needs_review counts as a positive candidate, not silently removed; unassessed positives count as misses.',
            'Train/dev only. Blind evaluation remains reserved; no human data are invented.',
            'No relevance semantics, factual summary, latency or final Agent score inferred from this keyword baseline.']}
    report['run_fingerprint']=digest(report);rid=uuid4()
    conn.execute(text('INSERT INTO annotation_runs(run_id,dataset_id,split,report) VALUES (:r,:d,:s,CAST(:v AS jsonb))'),
        {'r':rid,'d':identity,'s':split,'v':json.dumps(report,ensure_ascii=False)})
    return {'run_id':rid,**report}


def run_report(conn,identity,run_id):
    row=conn.execute(text('SELECT * FROM annotation_runs WHERE run_id=:r AND dataset_id=:d'),{'r':run_id,'d':identity}).mappings().one_or_none()
    if row is None:raise Missing('Run not found for dataset')
    return dict(row)
