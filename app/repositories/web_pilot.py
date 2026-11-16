"""Immutable extraction records, explicit attributed import, fixed public read queries."""
import hashlib,json,re
from uuid import UUID,uuid5,NAMESPACE_URL
from datetime import datetime,timezone
from sqlalchemy import text
from app.web_sources import SOURCES,get_source
from app.event_model import EventSnapshot
from app.repositories.events import save_snapshot

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def save_document(c,key,document,observed_at):
    source=get_source(key);source.url(document['url'],detail=True)
    if document['body_characters']!=len(document['body_text']) or document['body_sha256']!=hashlib.sha256(document['body_text'].encode()).hexdigest():raise ValueError('extraction_body_hash_mismatch')
    if not re.fullmatch('[a-f0-9]{64}',document['html_sha256']):raise ValueError('invalid_html_hash')
    lock=int.from_bytes(hashlib.sha256(('web-document:'+key+':'+document['url']).encode()).digest()[:8],'big',signed=True)
    c.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
    # Navigation/request-token changes must not create duplicate research documents.
    # Keep the first raw HTML hash and immutable full extraction for identical content/metadata.
    semantic={k:v for k,v in document.items() if k!='html_sha256'}
    duplicate=c.execute(text("SELECT document_id,document_hash,extraction FROM web_documents WHERE source_key=:key AND url=:url AND body_sha256=:body AND (extraction-'html_sha256')=CAST(:semantic AS jsonb) ORDER BY first_observed_at,document_id LIMIT 1"),
      {'key':key,'url':document['url'],'body':document['body_sha256'],'semantic':json.dumps(semantic,ensure_ascii=False)}).mappings().one_or_none()
    if duplicate:
        if digest(duplicate['extraction'])!=duplicate['document_hash']:raise ValueError('stored_document_hash_mismatch')
        return {'document_id':str(duplicate['document_id']),'created':False}
    fingerprint=digest(document);identifier=uuid5(NAMESPACE_URL,'pq-web-document:'+key+':'+document['url']+':'+fingerprint)
    inserted=c.execute(text('''INSERT INTO web_documents(document_id,source_key,url,title,body_sha256,html_sha256,document_hash,extraction,first_observed_at)
      VALUES(:id,:key,:url,:title,:body,:html,:hash,CAST(:payload AS jsonb),:at) ON CONFLICT DO NOTHING RETURNING document_id'''),
      {'id':identifier,'key':key,'url':document['url'],'title':document['title'],'body':document['body_sha256'],'html':document['html_sha256'],'hash':fingerprint,'payload':json.dumps(document,ensure_ascii=False),'at':observed_at}).scalar_one_or_none()
    stored=c.execute(text('SELECT document_hash FROM web_documents WHERE document_id=:id'),{'id':identifier}).scalar_one()
    if stored!=fingerprint:raise ValueError('document_identity_conflict')
    return {'document_id':str(identifier),'created':inserted is not None}

def save_run(c,report):
    c.execute(text('INSERT INTO web_pilot_runs(run_id,source_key,started_at,finished_at,status,report) VALUES(:id,:key,:start,:end,:status,CAST(:report AS jsonb))'),
      {'id':report['run_id'],'key':report['source'],'start':report['started_at'],'end':report['finished_at'],'status':report['status'],'report':json.dumps(report,ensure_ascii=False)})
    return {'run_id':report['run_id'],'status':report['status']}

def detail(c,identifier):
    row=c.execute(text('''SELECT d.*,l.record_id,l.event_id,l.version_id FROM web_documents d
      LEFT JOIN web_document_links l USING(document_id) WHERE document_id=:id'''),{'id':identifier}).mappings().one_or_none()
    return dict(row) if row else None

def listing(c,source=None,limit=20,before=None):
    return [dict(row) for row in c.execute(text('''SELECT d.document_id,d.source_key,d.url,d.title,d.first_observed_at,
      d.body_sha256,d.extraction->>'person_name' AS person_name,d.extraction->>'attribution' AS attribution,
      d.extraction->>'page_date' AS page_date,d.extraction->>'published_at' AS published_at,
      d.extraction->>'date_precision' AS date_precision,(d.extraction->>'body_characters')::integer AS body_characters,
      l.record_id,l.event_id,l.version_id FROM web_documents d LEFT JOIN web_document_links l USING(document_id)
      WHERE (CAST(:source AS text) IS NULL OR d.source_key=:source)
      AND (CAST(:before AS timestamptz) IS NULL OR d.first_observed_at<:before)
      ORDER BY d.first_observed_at DESC,d.document_id LIMIT :limit'''),{'source':source,'before':before,'limit':limit}).mappings()]

def overview(c):
    rows=[]
    for key,source in SOURCES.items():
        continuous=c.execute(text('SELECT enabled,last_finished_at FROM speed_sources WHERE source=:key'),{'key':key}).mappings().one_or_none()
        latest=c.execute(text('SELECT report FROM web_pilot_runs WHERE source_key=:key ORDER BY finished_at DESC,run_id DESC LIMIT 1'),{'key':key}).scalar_one_or_none()
        count=c.execute(text('SELECT count(*) FROM web_documents WHERE source_key=:key'),{'key':key}).scalar_one()
        rows.append({'source':key,'name':source.name,'publisher':source.publisher,'discovery_url':source.index,
          'catalog_number':source.catalog_number,'origin':'catalog' if source.catalog_number else 'supplemental_official',
          'policy_url':source.policy_url,'policy_note':source.policy_note,'document_count':count,'latest_run':latest,
          'continuous_collection_enabled':bool(continuous and continuous['enabled']),'last_continuous_scan_at':continuous['last_finished_at'] if continuous else None})
    return {'items':rows,'extractor':'Trafilatura','continuous_collection_enabled':any(row['continuous_collection_enabled'] for row in rows),'model_requests':0,
      'notice':'有限HTML试接：查看不会抓取网页。只有归属核对通过的讲话可显式加入人物库；机构背景单独展示。'}

def import_person(c,identifier):
    c.execute(text('SELECT pg_advisory_xact_lock(726932401)'))
    row=detail(c,identifier)
    if not row:raise ValueError('document_not_found')
    if row['record_id']:return {'action':'already_exists',**{k:str(row[k]) for k in ('document_id','record_id','event_id','version_id')}}
    document=row['extraction'];source=get_source(row['source_key']);source.url(document['url'],detail=True)
    # Only the inspected Fed speech template supplies a person identity. Headlines/LLM guesses never do.
    if source.key!='fed' or not document['eligible_for_person_import'] or document['attribution']!='official_speech_byline' or not document['person_name'] or not document['speaker_evidence']:raise ValueError('person_attribution_requires_review')
    if digest(document)!=row['document_hash'] or hashlib.sha256(document['body_text'].encode()).hexdigest()!=row['body_sha256']:raise ValueError('stored_document_hash_mismatch')
    person_id=uuid5(NAMESPACE_URL,'pq-official-person:'+document['person_name'])
    source_id=uuid5(NAMESPACE_URL,'pq-web-source:'+source.key)
    # Reuse an exact existing original name; ambiguity fails closed.
    people=c.execute(text('SELECT person_id FROM people WHERE name_original=:name'),{'name':document['person_name']}).scalars().all()
    if len(people)>1:raise ValueError('ambiguous_existing_person')
    if people:person_id=people[0]
    else:c.execute(text('INSERT INTO people(person_id,name_original,monitoring_enabled) VALUES(:id,:name,false)'),{'id':person_id,'name':document['person_name']})
    c.execute(text('''INSERT INTO sources(source_id,name,publisher_name,provenance_type,fetch_method,feed_url,homepage_url,enabled)
      VALUES(:id,:name,:publisher,'official','web_index',:url,:home,false) ON CONFLICT DO NOTHING'''),
      {'id':source_id,'name':source.name,'publisher':source.publisher,'url':source.index,'home':'https://'+source.host+'/'})
    configured=c.execute(text('SELECT provenance_type,fetch_method,feed_url FROM sources WHERE source_id=:id FOR SHARE'),{'id':source_id}).mappings().one()
    if (configured['provenance_type'],configured['fetch_method'],configured['feed_url'])!=('official','web_index',source.index):raise ValueError('source_configuration_changed')
    c.execute(text('INSERT INTO person_sources(person_id,source_id,enabled) VALUES(:person,:source,false) ON CONFLICT DO NOTHING'),{'person':person_id,'source':source_id})
    record_id=uuid5(NAMESPACE_URL,'pq-web-record:'+str(source_id)+':'+document['url'])
    # Lock per-source URL. Existing differing public records are preserved and held for review.
    existing=c.execute(text('SELECT record_id,body_text,person_id,title FROM public_records WHERE source_id=:source AND retrieved_url=:url FOR UPDATE'),{'source':source_id,'url':document['url']}).mappings().one_or_none()
    if existing and (existing['body_text']!=document['body_text'] or existing['person_id']!=person_id or existing['title']!=document['title']):raise ValueError('existing_record_changed_needs_review')
    if existing:record_id=existing['record_id']
    else:
        c.execute(text('''INSERT INTO public_records(record_id,source_id,person_id,title,content_type,provenance_type,
          original_url,retrieved_url,published_at,page_date,published_raw,first_seen_at,language,body_text,speaker_evidence,verification_status,content_hash)
          VALUES(:id,:source,:person,:title,'public_statement','official',:url,:url,:published,:day,:raw,:seen,'en',:body,:evidence,'verified_page_attribution',:hash)'''),
          {'id':record_id,'source':source_id,'person':person_id,'title':document['title'],'url':document['url'],'published':document['published_at'],'day':document['page_date'],'raw':document['published_raw'],'seen':row['first_observed_at'],'body':document['body_text'],'evidence':document['speaker_evidence'],'hash':document['body_sha256']})
    snapshot=EventSnapshot(title=document['title'],content_type='public_statement',post_type='speech',main_text=document['body_text'],
      original_url=document['url'],retrieved_url=document['url'],published_at=document['published_at'],page_date=document['page_date'],published_raw=document['published_raw'],language='en',person_id=person_id,
      reported_author=document['person_name'],speaker_evidence=document['speaker_evidence'],provenance_type='official',verification_status='verified_page_attribution',parser_version=document['parser_version']+'_'+document['extractor_version'])
    event=save_snapshot(c,source_id,'web_index',snapshot,datetime.now(timezone.utc),record_id=record_id,
      metadata={'web_document_id':str(identifier),'html_sha256':document['html_sha256'],'date_precision':document['date_precision'],'finite_pilot':True,'publication_latency_measured':False})
    c.execute(text('INSERT INTO web_document_links(document_id,record_id,event_id,version_id) VALUES(:doc,:record,:event,:version)'),{'doc':identifier,'record':record_id,'event':event['event_id'],'version':event['version_id']})
    return {'action':'inserted' if not existing else 'linked_existing','document_id':str(identifier),'record_id':str(record_id),'person_id':str(person_id),**event,'monitoring_enabled_changed':False,'source_enabled_changed':False,'model_requests':0}
