"""Fixed metadata filters and a persisted exact dense-vector index; no ANN claims."""
from datetime import datetime
import hashlib,json
from uuid import uuid4
from sqlalchemy import text
from app.annotation_model import digest
from app.local_embedding import SPEC,PROFILE_ID,DIMENSIONS,normalized

LATEST='''WITH current_versions AS (
 SELECT DISTINCT ON (event_id) event_id,version_id,observed_at FROM event_observations
 ORDER BY event_id,observed_at DESC,observation_id DESC)
'''
SELECT='''SELECT v.event_id,v.version_id,v.person_id,v.snapshot_hash,v.review_status,v.provenance_type,v.verification_status,
 ch.source_id,ch.transport,s.name AS source_name,p.name_original,p.name_zh,o.observed_at,
 v.payload->>'title' AS title,v.payload->>'main_text' AS body,v.payload->>'published_at' AS published_at,
 v.payload->>'original_url' AS original_url,v.payload->>'retrieved_url' AS retrieved_url
 FROM current_versions o JOIN event_versions v USING(event_id,version_id)
 JOIN source_channels ch USING(channel_id) JOIN sources s USING(source_id) JOIN people p ON p.person_id=v.person_id
'''
ELIGIBLE='''v.review_status='accepted' AND v.person_id IS NOT NULL
 AND v.payload->>'post_type' IN ('original','speech')
 AND v.payload->>'content_type' IN ('public_statement','institution_announcement')
 AND char_length(coalesce(v.payload->>'main_text','')) BETWEEN 1 AND 100000
 AND NOT EXISTS(SELECT 1 FROM annotation_samples a WHERE a.split='blind' AND a.origin='source'
   AND (a.event_key=v.event_id::text OR a.snapshot->>'event_version_id'=v.version_id::text))
'''

def lock(c):c.execute(text('SELECT pg_advisory_xact_lock(626184316)'))
def register(c):
    c.execute(text('INSERT INTO kb_profiles(profile_id,definition) VALUES (:id,CAST(:spec AS jsonb)) ON CONFLICT DO NOTHING'),{'id':PROFILE_ID,'spec':json.dumps(SPEC)})
    value=c.execute(text('SELECT definition FROM kb_profiles WHERE profile_id=:id'),{'id':PROFILE_ID}).scalar_one()
    if digest(value)!=PROFILE_ID:raise ValueError('Embedding profile differs')

def enqueue(c,limit=100):
    if type(limit) is not int or not 1<=limit<=300:raise ValueError('Index queue limit must be 1..300')
    lock(c);register(c)
    rows=c.execute(text(LATEST+SELECT+" LEFT JOIN kb_documents d ON d.version_id=v.version_id AND d.profile_id=:profile WHERE "+ELIGIBLE+" AND (d.version_id IS NULL OR d.state='skipped' AND d.failure_code='superseded_before_publish') ORDER BY v.event_id LIMIT :limit"),{'profile':PROFILE_ID,'limit':limit}).mappings().all()
    count=0
    for row in rows:
        if not row['body'].strip():continue
        values={'profile':PROFILE_ID,'version':row['version_id'],'event':row['event_id'],'person':row['person_id'],'source':row['source_id'],'snapshot':row['snapshot_hash'],'body':hashlib.sha256(row['body'].encode()).hexdigest()}
        c.execute(text("INSERT INTO kb_documents(profile_id,version_id,event_id,person_id,source_id,snapshot_hash,body_sha256) VALUES (:profile,:version,:event,:person,:source,:snapshot,:body) ON CONFLICT(profile_id,version_id) DO UPDATE SET state='queued',failure_code=NULL,lease_token=NULL,claimed_at=NULL WHERE kb_documents.state='skipped' AND kb_documents.failure_code='superseded_before_publish'"),values);count+=1
    return {'queued':count,'paid_api_calls':0}

def current(c,version_id):
    return c.execute(text(LATEST+SELECT+' WHERE '+ELIGIBLE+' AND v.version_id=:version'),{'version':version_id}).mappings().one_or_none()

def claim(c):
    lock(c)
    row=c.execute(text("SELECT * FROM kb_documents WHERE profile_id=:profile AND state='queued' ORDER BY created_at,version_id LIMIT 1 FOR UPDATE"),{'profile':PROFILE_ID}).mappings().one_or_none()
    if row is None:return None
    target=current(c,row['version_id'])
    if target is None:
        c.execute(text("UPDATE kb_documents SET state='skipped',failure_code='superseded_before_publish' WHERE profile_id=:profile AND version_id=:version"),{'profile':PROFILE_ID,'version':row['version_id']});return {'skipped':True}
    token=uuid4();c.execute(text("UPDATE kb_documents SET state='indexing',lease_token=:token,claimed_at=clock_timestamp() WHERE profile_id=:profile AND version_id=:version"),{'token':token,'profile':PROFILE_ID,'version':row['version_id']})
    return {**dict(target),'lease_token':token,'body_sha256':row['body_sha256']}

def cached(c,hashes):
    if not hashes:return {}
    rows=c.execute(text('SELECT DISTINCT ON(text_sha256) text_sha256,embedding FROM kb_chunks WHERE profile_id=:profile AND text_sha256=ANY(CAST(:hashes AS text[])) ORDER BY text_sha256,chunk_id'),{'profile':PROFILE_ID,'hashes':hashes}).mappings().all()
    return {row['text_sha256']:normalized(row['embedding']) for row in rows}

def publish(c,target,parts,vectors):
    lock(c);params={'profile':PROFILE_ID,'version':target['version_id']}
    row=c.execute(text('SELECT * FROM kb_documents WHERE profile_id=:profile AND version_id=:version FOR UPDATE'),params).mappings().one()
    if row['state']!='indexing' or row['lease_token']!=target['lease_token']:raise ValueError('Index lease no longer owned')
    now=current(c,target['version_id'])
    if now is None:
        c.execute(text("UPDATE kb_documents SET state='skipped',failure_code='superseded_before_publish',lease_token=NULL WHERE profile_id=:profile AND version_id=:version"),params);return {'state':'skipped','reason':'superseded_before_publish'}
    if now['snapshot_hash']!=row['snapshot_hash'] or hashlib.sha256(now['body'].encode()).hexdigest()!=row['body_sha256']:raise ValueError('Frozen source body changed')
    if len(parts)!=len(vectors) or not 1<=len(parts)<=2000:raise ValueError('Incomplete chunk/vector batch')
    last=0
    for index,(part,vector) in enumerate(zip(parts,vectors)):
        if part['start']<0 or part['start']>last or part['end']<=last or part['text']!=now['body'][part['start']:part['end']] or hashlib.sha256(part['text'].encode()).hexdigest()!=part['text_sha256']:raise ValueError('Chunk coverage or verbatim source differs')
        if index==0 and part['start']!=0:raise ValueError('Source beginning omitted')
        last=part['end'];vector=normalized(vector)
        c.execute(text('INSERT INTO kb_chunks(chunk_id,profile_id,version_id,start_offset,end_offset,text,token_count,text_sha256,embedding) VALUES (:id,:profile,:version,:start,:end,:text,:tokens,:hash,CAST(:vector AS real[]))'),{'id':uuid4(),**params,'start':part['start'],'end':part['end'],'text':part['text'],'tokens':part['tokens'],'hash':part['text_sha256'],'vector':vector})
    if last!=len(now['body']):raise ValueError('Source ending omitted')
    c.execute(text("UPDATE kb_documents SET state='ready',chunk_count=:count,indexed_at=clock_timestamp(),lease_token=NULL,failure_code=NULL WHERE profile_id=:profile AND version_id=:version"),{**params,'count':len(parts)})
    return {'state':'ready','version_id':str(target['version_id']),'chunks':len(parts)}

def fail(c,target,code='local_index_failed'):
    lock(c);c.execute(text("UPDATE kb_documents SET state='failed',failure_code=:code,lease_token=NULL WHERE profile_id=:profile AND version_id=:version AND lease_token=:token AND state='indexing'"),{'profile':PROFILE_ID,'version':target['version_id'],'token':target['lease_token'],'code':code})

def reset_unsent(c,version_id):
    # CPU-only recovery is explicit; changing the token fences a late old writer.
    lock(c);count=c.execute(text("UPDATE kb_documents SET state='queued',lease_token=NULL,failure_code=NULL WHERE profile_id=:profile AND version_id=:version AND state IN ('failed','indexing')"),{'profile':PROFILE_ID,'version':version_id}).rowcount
    return {'reset':count,'paid_api_calls':0}

def status(c):
    counts=dict(c.execute(text(LATEST+'''SELECT coalesce(d.state,'not_queued'),count(*)
      FROM current_versions o JOIN event_versions v USING(event_id,version_id)
      LEFT JOIN kb_documents d ON d.version_id=v.version_id AND d.profile_id=:profile
      WHERE '''+ELIGIBLE+" GROUP BY coalesce(d.state,'not_queued')"),{'profile':PROFILE_ID}).all())
    versions=c.execute(text('SELECT count(*) FROM kb_documents WHERE profile_id=:profile'),{'profile':PROFILE_ID}).scalar_one()
    chunks=c.execute(text('SELECT count(*) FROM kb_chunks WHERE profile_id=:profile'),{'profile':PROFILE_ID}).scalar_one()
    control=dict(c.execute(text('SELECT * FROM kb_state WHERE singleton')).mappings().one())
    return {'profile_id':PROFILE_ID,'model':SPEC['model'],'model_revision':SPEC['revision'],'dimensions':DIMENSIONS,'engine':'postgresql_dense_flat_exact_cosine','eligible_latest_versions':sum(counts.values()),'latest_states':counts,'stored_chunks_including_history':chunks,'stored_versions_including_history':versions,'control':control,'paid_api_calls':0,'rag_chat_connected':True,'scope':'saved_accepted_main_text_only'}

def candidates(c,person_id,since=None,until=None,head=None,query=None):
    params={'profile':PROFILE_ID,'person':person_id,'since':since,'until':until,'head':head,'q':query or ''}
    latest=LATEST if head is None else LATEST.replace('FROM event_observations\n','FROM event_observations WHERE observation_id<=:head\n')
    rows=c.execute(text(latest+'''SELECT k.*,v.event_id,v.person_id,v.snapshot_hash,v.provenance_type,v.verification_status,
      ch.source_id,ch.transport,s.name AS source_name,p.name_original,p.name_zh,v.payload->>'title' AS title,
      v.payload->>'published_at' AS published_at,v.payload->>'original_url' AS original_url,v.payload->>'retrieved_url' AS retrieved_url
      FROM current_versions o JOIN event_versions v USING(event_id,version_id)
      JOIN source_channels ch USING(channel_id) JOIN sources s USING(source_id) JOIN people p ON p.person_id=v.person_id
      JOIN kb_documents d ON d.version_id=v.version_id AND d.profile_id=:profile AND d.state='ready'
      JOIN kb_chunks k ON k.profile_id=d.profile_id AND k.version_id=d.version_id
      WHERE '''+ELIGIBLE+''' AND v.person_id=:person AND v.snapshot_hash=d.snapshot_hash
      AND substring(v.payload->>'main_text' FROM k.start_offset+1 FOR k.end_offset-k.start_offset)=k.text
      AND (CAST(:since AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz>=:since)
      AND (CAST(:until AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz<:until)
      AND (:q='' OR strpos(lower(coalesce(v.payload->>'main_text','')),lower(:q))>0 OR strpos(lower(v.payload->>'title'),lower(:q))>0)
      ORDER BY v.event_id,k.start_offset LIMIT 10001'''),params).mappings().all()
    if len(rows)>10000:raise ValueError('Exact index scope exceeds 10000 chunks; narrow person/date scope')
    return [dict(row) for row in rows]

def control(c,enabled):
    lock(c);register(c);c.execute(text('UPDATE kb_state SET enabled=:enabled,profile_id=:profile,pause_reason=NULL WHERE singleton'),{'enabled':enabled,'profile':PROFILE_ID})
    return {'enabled':enabled,'profile_id':PROFILE_ID,'paid_api_calls':0}

def heartbeat(c):
    c.execute(text('UPDATE kb_state SET checked_at=clock_timestamp() WHERE singleton'))
    return dict(c.execute(text('SELECT * FROM kb_state WHERE singleton')).mappings().one())
