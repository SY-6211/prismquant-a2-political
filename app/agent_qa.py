"""Bounded person QA: prepare evidence, validate grounded points, no implicit spend."""
import json
import hashlib
from datetime import datetime
from types import SimpleNamespace
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime,Field,model_validator,ValidationError
from app.agent_tools import Arguments,ToolSession,open_session,encoded
from app.agent_analysis import MODEL,Quote
from app.annotation_model import digest
from app.model_trial import load_json
from app.repositories import chat,qa as repo

from app import qa_prompts, agent_memory, agent_plan, research_synthesis
from app.research_answer import ResearchAnswer,ResearchOutput,ResearchResponse,ResearchStep,Inference,empty_research,narrative
from app.qa_prompts import BASE_SYSTEM,CLOCK_RULES,LEGACY_V3_SYSTEM
PROMPT_VERSION=qa_prompts.CURRENT
SUPPORTED_PROMPTS=qa_prompts.SUPPORTED
EXECUTABLE_PROMPTS=qa_prompts.EXECUTABLE
SYSTEM=qa_prompts.system(PROMPT_VERSION)
MAX_CONTEXT=32768


class Prepare(Arguments):
    client_request_id:UUID
    message_id:UUID
    retrieval_mode:Literal['literal','hybrid']='literal'
    research_mode:Literal['auto','focused','summary','compare','evidence','research']='auto'
    use_history:bool=Field(default=True,strict=True)
    read_official_sources:bool=Field(default=False,strict=True)
    query:str|None=Field(default=None,min_length=1,max_length=120)
    since:AwareDatetime|None=None
    until:AwareDatetime|None=None
    limit:int=Field(default=3,ge=1,le=6,strict=True)
    event_id:UUID|None=None
    version_id:UUID|None=None

    @model_validator(mode='after')
    def scope(self):
        if self.since and self.until and self.since>=self.until:raise ValueError('Invalid date interval')
        if (self.event_id is None)!=(self.version_id is None):raise ValueError('Specify event and version together')
        if self.event_id and (self.query or self.since or self.until):raise ValueError('Exact version and search filters are exclusive')
        if self.query is not None and not self.query.strip():raise ValueError('Nonblank search query required')
        if self.limit>3 and (self.retrieval_mode=='literal' or self.event_id):raise ValueError('Legacy/exact retrieval supports at most three sources')
        if self.research_mode!='auto' and (self.retrieval_mode!='hybrid' or self.event_id):raise ValueError('Research skills require hybrid search')
        if self.read_official_sources and (self.retrieval_mode!='hybrid' or self.event_id):raise ValueError('Official navigation requires hybrid question routing')
        return self


class Point(Arguments):
    text_zh:str=Field(min_length=1,max_length=400)
    citations:list[Quote]=Field(min_length=1,max_length=2)


class Answer(Arguments):
    outcome:Literal['answered','insufficient_evidence']
    points:list[Point]=Field(max_length=4)
    limitations_zh:list[str]=Field(min_length=1,max_length=4)
    needs_review:bool

    @model_validator(mode='after')
    def consistency(self):
        if (self.outcome=='answered')!=bool(self.points):raise ValueError('Outcome needs cited points or abstention')
        if any(not p.text_zh.strip() for p in self.points) or any(not v.strip() or len(v)>300 for v in self.limitations_zh):raise ValueError('Invalid answer text')
        return self


def queries_for(question,explicit):
    if explicit:return [explicit],'explicit_literal'
    groups=[(('关税','贸易','tariff','trade'),['tariff','trade']),
            (('降息','加息','利率','rate cut','interest rate'),['interest rate','rate cut']),
            (('通胀','inflation'),['inflation']),
            (('选举','股市','election','stock market'),['election','stock market'])]
    values=[]
    for terms,queries in groups:
        if any(term in question.lower() for term in terms):values.extend(queries)
    return list(dict.fromkeys(values))[:3] or [''],'limited_bilingual_terms' if values else 'recent_saved_fallback'


def prepare_context(session,question,body):
    sources=[];candidate_count=0;skipped=0;hybrid=None;plan=None
    history,memory=agent_memory.select(question['content'],question['history'] if body.use_history else [],question.get('history_window_truncated',False) if body.use_history else False)
    memory['enabled']=body.use_history
    if body.query or body.event_id:memory['unresolved_followup']=False
    routing=agent_plan.route(question['content'],body.research_mode,memory)
    source_lookup=None
    if body.retrieval_mode=='hybrid' and not body.event_id and routing['task']=='source_lookup':
        from app import research_sources
        source_lookup=research_sources.prepare(question['content'],memory,enabled=body.read_official_sources)
        state=memory['research_state'];memory['research_state']={k:v for k,v in state.items() if k not in ('items','checklist')}|{'item_count':len(state.get('items',[]))}
        candidates=[];queries=[];mode='official_source_navigation'
        plan={'version':'official_source_navigation_v1','strategy':'research','selected_by':routing['selected_by'],'routing':routing,
              'model_planner_calls':0,'tool_call_limit':3,'source_limit':len(source_lookup['resources']),
              'steps':[{'number':1,'tool':'find_official_sources','purpose':'map_checklist_to_official_entries','status':'ok'},
                       {'number':2,'tool':'read_official_pages','purpose':'read_bounded_official_entries','status':'ok' if any(s['status']=='read' for s in source_lookup['resources']) else 'not_read'}],
              'read_only':True,'extra_reads':0,'stop_reason':source_lookup['stop_reason']}
    elif body.event_id:
        candidates=[{'event_id':str(body.event_id),'version_id':str(body.version_id)}];queries=[];mode='exact_version'
    elif body.retrieval_mode=='hybrid':
        sources,hybrid,plan=agent_plan.retrieve(session,question,body,memory)
        candidates=[];queries=hybrid['queries'];mode=hybrid['mode']
    else:
        queries,mode=queries_for(question['content'],body.query);found={}
        for query in queries:
            page=session.invoke('search_statements',{'person_id':str(question['person_id']),'query':query,
                'since':body.since,'until':body.until,'limit':body.limit})
            if page.get('status')!='ok':raise ValueError('Retrieval unavailable')
            for row in page['items']:found[(row['event_id'],row['version_id'])]=row
        candidates=list(found.values())[:body.limit]
    candidate_count=hybrid['candidate_count'] if hybrid else len(candidates)
    for row in candidates:
        start=row['start_offset'] if hybrid else 0
        length=row['end_offset']-start if hybrid else 1500
        value=session.invoke('read_version',{'event_id':row['event_id'],'version_id':row['version_id'],'start':start,'length':length})
        if value.get('status')!='ok':skipped+=1;continue
        source=value['source']
        if hybrid and (source['snapshot_hash']!=row['snapshot_hash'] or source['text_sha256']!=row['text_sha256'] or source['end']!=row['end_offset']):
            raise ValueError('Retrieved chunk differs from exact source version')
        if getattr(session.reader,'is_reserved',lambda s:False)(source):skipped+=1;continue
        sources.append(source)
    context={'prompt_version':PROMPT_VERSION,'conversation_id':str(question['conversation_id']),
        'message_id':str(question['message_id']),'person_id':str(question['person_id']),'question':question['content'],
        'question_position':question['position'],'prior_conversation':history,'memory':memory,
        'history_is_evidence':False,'sources':sources,'captured_at':session.reader.captured_at,
        'retrieval':{**(hybrid or {}),'mode':mode,'queries':queries,'since':body.since,'until':body.until,
            'candidate_count':candidate_count,'skipped':skipped,'limit':body.limit,'characters_per_source':hybrid.get('characters_per_source',1000) if hybrid else 1500,
            'exhaustive':False,'semantic_vector_search':bool(hybrid and hybrid['semantic_vector_search']),'historical_availability_verified':False},
        'tool_audit':session.audit}
    if plan is None:
        routing=agent_plan.route(question['content'],body.research_mode,memory)
        if routing['task']=='source_lookup':routing={**routing,'task':'method_checklist'}
        strategy,origin=routing['strategy'],routing['selected_by']
        plan={'version':agent_plan.VERSION,'strategy':strategy,'selected_by':origin,'routing':routing,'execution_path':'compatible_literal_or_exact',
              'steps':[],'model_planner_calls':0,'read_only':True,'tool_call_limit':8,'source_limit':body.limit,'extra_reads':0,'stop_reason':'completed'}
    context['plan']=plan
    if plan['strategy']=='research':
        context['prompt_version']=qa_prompts.SOURCE_NAVIGATION if source_lookup is not None else qa_prompts.RESEARCH
        context['research_task']=plan['routing']['task']
    if source_lookup is not None:context['source_lookup']=source_lookup
    if body.retrieval_mode=='hybrid' and body.read_official_sources and not body.event_id and source_lookup is None and plan['strategy'] in ('research','summary','compare'):
        research_synthesis.prepare(context,question,body)
        context['quote_catalog']=research_synthesis.quote_catalog(context)
    context['prompt_contract']=qa_prompts.manifest(context['prompt_version'],'research' if context['prompt_version']==qa_prompts.SYNTHESIS else plan['strategy'],context.get('research_task','impact_analysis'))
    context['retrieval']['omitted_due_context_limit']=0
    while len(encoded(context))>MAX_CONTEXT and context['prior_conversation']:
        context['prior_conversation'].pop(0);memory['omitted_for_context_budget']+=1;memory['selected']=len(context['prior_conversation'])
    while len(encoded(context))>MAX_CONTEXT and len(sources)>1:
        sources.pop();context['retrieval']['omitted_due_context_limit']+=1
        if 'quote_catalog' in context:context['quote_catalog']=research_synthesis.quote_catalog(context)
    while len(encoded(context))>MAX_CONTEXT and context.get('background_sources'):
        context['background_sources'].pop();context['research_workflow']['omitted_background_for_context_budget']=True
        if 'quote_catalog' in context:context['quote_catalog']=research_synthesis.quote_catalog(context)
    if context.get('research_workflow'):
        context['research_workflow']['frozen_counts']={'person':len(sources),'background':len(context['background_sources'])}
    if source_lookup:
        while len(encoded(context))>MAX_CONTEXT:
            candidates=[r for r in source_lookup['resources'] if len(r.get('text',''))>120]
            if not candidates:break
            item=max(candidates,key=lambda r:len(r['text']));item['text']=item['text'][:len(item['text'])//2]
            item['text_sha256']=hashlib.sha256(item['text'].encode()).hexdigest();item['excerpt_truncated']=True
            if 'evidence_snippets' in item:
                item['text_end']=item['text_start']+len(item['text'])
                item['evidence_snippets']=[s for s in item['evidence_snippets'] if s['end']<=item['text_end']]
    if len(encoded(context))>MAX_CONTEXT:raise ValueError('Context exceeds budget')
    return json.loads(encoded(context))


def request_payload(context):
    version=context['prompt_version']
    if version==qa_prompts.SOURCE_NAVIGATION:raise ValueError('Source navigation does not call a model')
    if version not in SUPPORTED_PROMPTS:raise ValueError('Unsupported frozen prompt')
    messages=[{'role':'system','content':qa_prompts.checked(context)}]
    if version!='person_qa_v2':
        captured=datetime.fromisoformat(context['captured_at'].replace('Z','+00:00'))
        if captured.tzinfo is None:raise ValueError('Trusted clock requires timezone')
        messages.append({'role':'system','content':'Trusted retrieval clock: '+captured.isoformat()+'. This is retrieval time, not a publication timestamp or independent factual verification.'})
    messages.append({'role':'user','content':json.dumps(context,ensure_ascii=False,separators=(',',':'))})
    return {'model':MODEL,'messages':messages,
        'max_tokens':qa_prompts.output_tokens(version),'temperature':0,'response_format':{'type':'json_schema','json_schema':{
            'name':'person_qa','strict':True,'schema':answer_model(context).model_json_schema()}},
        'provider':{'allow_fallbacks':False,'require_parameters':True,'data_collection':'deny','max_price':{'prompt':1,'completion':2}},
        'reasoning':{'enabled':False},'include_reasoning':False}


def answer_model(context):
    if context['prompt_version'] in qa_prompts.SYNTHESIS_SELECTION_VERSIONS:return research_synthesis.SelectedAnswer
    if context['prompt_version']==qa_prompts.SYNTHESIS_V1:return research_synthesis.Answer
    if context['prompt_version']==qa_prompts.RESEARCH:return ResearchResponse
    if context['prompt_version']==qa_prompts.RESEARCH_V3:return ResearchOutput
    return ResearchAnswer if qa_prompts.is_research(context['prompt_version']) else Answer


def raw_answer(context,answer):
    """Rebuild provider-shaped content for receipt reconciliation, retaining research fields."""
    if context['prompt_version']==qa_prompts.SOURCE_NAVIGATION:return answer
    if context['prompt_version'] in qa_prompts.SYNTHESIS_SELECTION_VERSIONS:return research_synthesis.raw_selected(answer)
    if context['prompt_version']==qa_prompts.SYNTHESIS_V1:return research_synthesis.raw(answer)
    value={key:answer[key] for key in answer_model(context).model_fields}
    value['points']=[{'text_zh':p['text_zh'],'citations':[{'source_ref':q['source_ref'],'text':q['text']} for q in p['citations']]} for p in answer['points']]
    if context['prompt_version']==qa_prompts.RESEARCH:
        value['analysis']=[{k:p[k] for k in Inference.model_fields} for p in answer['analysis']]
        value['research_steps']=[{k:p[k] for k in ResearchStep.model_fields} for p in answer['research_steps']]
    return value


def abstention(context,limitation):
    if context['prompt_version'] in qa_prompts.SYNTHESIS_VERSIONS:return research_synthesis.abstention(context,limitation)
    value=dict(outcome='insufficient_evidence',points=[],limitations_zh=[limitation],needs_review=True)
    if qa_prompts.is_research(context['prompt_version']):value.update(empty_research())
    if context['prompt_version'] in (qa_prompts.RESEARCH_V3,qa_prompts.RESEARCH):value.pop('outcome')
    if context['prompt_version']==qa_prompts.RESEARCH:value['research_steps']=[]
    return validate_answer(context,value)


def validate_answer(context,value):
    if context['prompt_version'] not in SUPPORTED_PROMPTS:raise ValueError('Frozen QA prompt version differs')
    qa_prompts.checked(context)
    if context['prompt_version'] in qa_prompts.SYNTHESIS_SELECTION_VERSIONS:return research_synthesis.validate_selected(context,value)
    if context['prompt_version']==qa_prompts.SYNTHESIS_V1:return research_synthesis.validate(context,value)
    if context['prompt_version']==qa_prompts.SOURCE_NAVIGATION:
        from app import research_sources
        return research_sources.validate(context,value)
    answer=answer_model(context).model_validate(value);sources={s['source_ref']:s for s in context['sources']}
    if context['prompt_version']==qa_prompts.RESEARCH:
        if context['research_task']=='impact_analysis' and answer.research_steps:raise OutputRejected('qa_schema_rejected',{'rule':'impact_requires_source_analysis'})
        if context['research_task']=='method_checklist' and answer.outcome=='answered' and not answer.research_steps:raise OutputRejected('qa_schema_rejected',{'rule':'method_checklist_requires_labeled_steps'})
    session=ToolSession(SimpleNamespace(captured_at=context['captured_at']),[UUID(context['person_id'])]);session.segments=sources
    result=answer.model_dump();result['outcome']=answer.outcome
    for original,out in zip(answer.points,result['points']):
        response=session.invoke('validate_citations',{'citations':[{'source_ref':q.source_ref,'text':q.text,
            'use_as':'main_text','subject_person_id':context['person_id']} for q in original.citations]})
        if not response.get('citations_valid'):
            known={'source_not_read_in_this_session','content_boundary_mismatch','subject_attribution_not_established','quotation_not_unique_in_read_segment','outside_read_segment','quotation_mismatch'}
            raise OutputRejected('qa_citation_rejected',{'reasons':sorted({r.get('reason') if r.get('reason') in known else 'citation_check_failed' for r in response.get('checks',[]) if not r.get('valid')})})
        out['citations']=[{**check,'text':quote.text,'original_url':sources[quote.source_ref].get('original_url'),
            'retrieved_url':sources[quote.source_ref].get('retrieved_url')} for quote,check in zip(original.citations,response['checks'])]
        if context['prompt_version']==qa_prompts.RESEARCH:
            for q in out['citations']:
                source=sources[q['source_ref']];a=q['start']-source['start'];b=q['end']-source['start'];text=source['text']
                word=lambda c:c.isascii() and c.isalnum()
                if a>0 and word(text[a-1]) and word(text[a]):raise OutputRejected('qa_citation_rejected',{'reasons':['quotation_starts_in_word']})
                if b<len(text) and word(text[b-1]) and word(text[b]):raise OutputRejected('qa_citation_rejected',{'reasons':['quotation_ends_in_word']})
    result.update(answer_zh='\n\n'.join(p.text_zh for p in answer.points) if answer.points else '当前检索范围内没有足够原文证据，无法可靠回答。',
        needs_review=answer.needs_review or bool(context['retrieval'].get('includes_historical_versions')) or any(s['truncated'] or s['provenance_type']=='third_party_archive' or s['review_status']!='accepted' for s in sources.values()),
        human_review_status='unreviewed',factual_support='not_independently_assessed',
        retrieval_scope=context['retrieval'],historical_availability_verified=False)
    if context.get('memory',{}).get('unresolved_followup') and answer.outcome=='insufficient_evidence':
        result['answer_zh']='请先说明要继续研究的主题，或提出一个具体问题；这次没有取得可用原文依据。'
    if qa_prompts.is_research(context['prompt_version']):
        result.update(answer_mode='research',needs_review=True,inference_support='not_independently_assessed',
                      validation_scope='schema_premise_links_and_exact_quotes_only')
        if answer.outcome=='answered':result['answer_zh']=narrative(result)
        if context['prompt_version'] in (qa_prompts.RESEARCH_V3,qa_prompts.RESEARCH):result['outcome_classified_by']='host_validated_structure_not_model_label'
        if context['prompt_version']==qa_prompts.RESEARCH:
            result['research_task']=context['research_task']
            def citations(indices):
                found={}
                for i in indices:
                    for q in result['points'][i-1]['citations']:found[(q['source_ref'],q['start'],q['end'])]=q
                return list(found.values())
            for item in result['analysis']:item['evidence_citations']=citations(item['premise_points'])
            for item in result['research_steps']:item['evidence_citations']=citations(item['premise_points'])
    return result


class OutputRejected(ValueError):
    def __init__(self,code,details=None):self.code=code;self.details=details or {};super().__init__(code)


def validate_envelope(context,envelope):
    choices=envelope.get('choices')
    if envelope.get('model')!=MODEL or not isinstance(choices,list) or len(choices)!=1:raise OutputRejected('qa_response_contract_rejected')
    if choices[0].get('finish_reason')=='length':raise OutputRejected('qa_output_truncated')
    if choices[0].get('finish_reason')!='stop':raise OutputRejected('qa_response_incomplete')
    message=choices[0].get('message',{});content=message.get('content')
    if message.get('tool_calls') or message.get('function_call') or message.get('refusal') or not isinstance(content,str) or len(content.encode())>32768:raise ValueError('Unexpected response')
    try:value=load_json(content)
    except ValueError:raise OutputRejected('qa_invalid_json') from None
    try:return validate_answer(context,value)
    except ValidationError as error:
        schema=answer_model(context).model_json_schema();allowed=set(schema.get('properties',{}))
        for definition in schema.get('$defs',{}).values():allowed.update(definition.get('properties',{}))
        fields=[{'type':r['type'],'field':[v if type(v) is int or v in allowed else '[field]' for v in r['loc']]} for r in error.errors(include_input=False,include_context=False,include_url=False)[:8]]
        raise OutputRejected('qa_schema_rejected',{'fields':fields}) from None


def prepare(settings,token,conversation_id,body):
    # Omit the newly introduced default to preserve pre-RAG retry fingerprints.
    exclude={'client_request_id'}|({'retrieval_mode'} if body.retrieval_mode=='literal' else set())|({'research_mode'} if body.research_mode=='auto' else set())|({'use_history'} if body.use_history else set())|({'read_official_sources'} if not body.read_official_sources else set())
    spec=json.loads(body.model_dump_json(exclude=exclude));fingerprint=digest(spec)
    existing=chat.execute(settings,token,repo.by_request,conversation_id,body.client_request_id)
    if existing:
        if existing['request_hash']!=fingerprint:raise chat.Conflict('相同提交标识已用于不同问答范围。')
        return existing
    question=chat.execute(settings,token,repo.question,conversation_id,body.message_id,body.use_history)
    with open_session(settings,[question['person_id']]) as session:context=prepare_context(session,question,body)
    return chat.execute(settings,token,repo.save_prepared,conversation_id,body.client_request_id,spec,fingerprint,context,MODEL,context['prompt_version'])
