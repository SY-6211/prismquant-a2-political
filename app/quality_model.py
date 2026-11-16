"""Human judgments and mechanical audits are deliberately different measurements."""
import hashlib
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.annotation_model import Evidence, digest

VERSION = 'person_quality_v1'
FeedbackKind = Literal['useful', 'unsupported', 'wrong_person', 'time_error', 'missed_context', 'irrelevant', 'other']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class Feedback(Strict):
    client_request_id: UUID
    kind: FeedbackKind
    note: str = Field(min_length=1, max_length=1000)

    @field_validator('note')
    @classmethod
    def clean(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError('Explain the feedback using printable text')
        return value.strip()


class Relevance(Strict):
    version_id: UUID
    relevant: bool = Field(strict=True)
    evidence: list[Evidence] = Field(default_factory=list, max_length=3)


class Review(Strict):
    expected_revision: int = Field(default=0, ge=0, strict=True)
    independent_human_review: bool = Field(strict=True)
    relevance: list[Relevance] = Field(max_length=12)
    supported: bool = Field(strict=True)
    person_correct: bool = Field(strict=True)
    time_correct: bool = Field(strict=True)
    useful: bool = Field(strict=True)
    note: str = Field(min_length=1, max_length=1000)

    _clean = field_validator('note')(Feedback.clean.__func__)

    @model_validator(mode='after')
    def human(self):
        if not self.independent_human_review:
            raise ValueError('Only an explicitly attested human review may be submitted')
        return self

    def judgment(self):
        return {**self.model_dump(exclude={'expected_revision', 'note', 'independent_human_review', 'relevance'}),
                'relevance': sorted([(str(x.version_id), x.relevant) for x in self.relevance])}

    def validate_packet(self, packet):
        pool = {x['version_id']: x for x in packet['pool']}
        ids = [str(x.version_id) for x in self.relevance]
        if len(ids) != len(set(ids)) or set(ids) != set(pool):
            raise ValueError('Review every frozen candidate once, including non-retrieved candidates')
        for item in self.relevance:
            if item.relevant and not item.evidence:
                raise ValueError('Relevant versions require an exact main-text quotation')
            body = pool[str(item.version_id)]['body']
            for evidence in item.evidence:
                if evidence.end <= evidence.start or body[evidence.start:evidence.end] != evidence.text:
                    raise ValueError('Review evidence differs from the frozen Unicode text')


def audit(run):
    """Check frozen identity/offset/hash contracts; never infer semantic support."""
    context, answer = run['context'], run.get('answer')
    errors, unavailable = [], []
    if digest(context) != run['context_hash']:
        errors.append('context_hash')
    from app import qa_prompts
    if context.get('prompt_version') not in qa_prompts.SUPPORTED:
        unavailable.append('legacy_prompt_not_in_current_registry')
    else:
        try:
            qa_prompts.checked(context)
        except (ValueError, KeyError, TypeError):
            errors.append('prompt_contract')
    if answer is None:
        return {'state': 'failed' if errors else 'no_answer', 'errors': errors, 'unavailable': unavailable, 'semantic_support': 'not_assessed'}
    if run.get('execution_kind') in ('local_abstention','local_source_navigation','synthetic_fixture'):
        if digest(answer) != run['response_hash']:
            errors.append('answer_hash')
    else:
        # Existing provider executions hash the entire API envelope, not the
        # enriched, parsed answer. Do not compare different artifacts or invent it.
        unavailable.append('provider_envelope_hash_not_recomputable_from_parsed_answer')
    sources = {s['source_ref']: s for s in context.get('sources', [])}
    if len(sources) != len(context.get('sources', [])):
        errors.append('duplicate_source_ref')
    for source in sources.values():
        if source.get('person_id') != context.get('person_id') or source.get('role') != 'main_text':
            errors.append('source_identity')
        if source.get('text_sha256') != hashlib.sha256(source['text'].encode()).hexdigest():
            errors.append('source_segment_hash')
    count = 0
    if qa_prompts.is_research(context.get('prompt_version')) or context.get('prompt_version')==qa_prompts.SOURCE_NAVIGATION:
        from app.agent_qa import raw_answer,validate_answer
        try:
            if digest(validate_answer(context,raw_answer(context,answer)))!=digest(answer):errors.append('research_answer_contract')
        except (ValueError,KeyError,TypeError):errors.append('research_answer_contract')
    for point in answer.get('points', []):
        if not point.get('citations'):
            errors.append('uncited_point')
        for quote in point.get('citations', []):
            count += 1
            source = sources.get(quote.get('source_ref'))
            if not source:
                errors.append('unknown_source'); continue
            start, end = quote.get('start'), quote.get('end')
            if (quote.get('valid') is not True or quote.get('role') != 'main_text'
                    or any(quote.get(k) != source.get(k) for k in ('event_id', 'version_id', 'snapshot_hash'))):
                errors.append('citation_identity')
            if (type(start) is not int or type(end) is not int
                    or not source['start'] <= start < end <= source['end']
                    or source['text'][start-source['start']:end-source['start']] != quote.get('text')):
                errors.append('citation_text')
    if context.get('prompt_version') in qa_prompts.SYNTHESIS_VERSIONS and 'research_answer_contract' not in errors:
        count+=sum(len(p.get('citations',[])) for p in answer.get('background_points',[]))
    method_only=((context.get('prompt_version') in (qa_prompts.RESEARCH,*qa_prompts.SYNTHESIS_VERSIONS) and context.get('research_task')=='method_checklist' and bool(answer.get('research_steps')) or context.get('prompt_version')==qa_prompts.SOURCE_NAVIGATION and bool(answer.get('source_resources'))) and 'research_answer_contract' not in errors)
    if run['status'] == 'completed' and not count and not method_only:
        errors.append('empty_completed_answer')
    return {'state': 'failed' if errors else 'partial' if unavailable else 'passed', 'errors': sorted(set(errors)),
            'unavailable': unavailable, 'answer_payload_sha256': digest(answer),
            'citations': count, 'semantic_support': 'not_assessed'}


def metrics(packet, label):
    relevant = {str(x.version_id) for x in label.relevance if x.relevant}
    retrieved = set(packet['retrieved_version_ids'])
    hit = len(relevant & retrieved)
    return {'precision_in_pool': hit / len(retrieved) if retrieved else None,
            'recall_in_pool': hit / len(relevant) if relevant else None,
            'relevant': len(relevant), 'retrieved': len(retrieved), 'hit': hit,
            **{key: getattr(label, key) for key in ('supported', 'person_correct', 'time_correct', 'useful')}}
