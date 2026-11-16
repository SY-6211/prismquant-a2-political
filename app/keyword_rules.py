"""Deterministic, bounded keyword hints. Never a relevance or factual verdict."""
import hashlib
import re
import unicodedata
from bisect import bisect_right
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENGINE_VERSION = 'keyword_v1'


def normalize(value):
    return unicodedata.normalize('NFKC', value).casefold()


class Rule(BaseModel):
    model_config = ConfigDict(extra='forbid', revalidate_instances='always')
    name: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=100)
    person_ids: list[UUID] = Field(default_factory=list, max_length=20)
    # Within a group terms are synonyms (OR); between groups use match_mode.
    groups: list[list[str]] = Field(min_length=1, max_length=12)
    match_mode: Literal['any', 'all'] = 'any'
    context_any: list[str] = Field(default_factory=list, max_length=30)
    exclude_any: list[str] = Field(default_factory=list, max_length=30)
    window: int = Field(default=160, ge=30, le=500)
    severity: Literal['low', 'medium', 'high'] = 'medium'
    notification: Literal['none', 'in_app_after_review', 'in_app_literal_hint'] = 'none'
    flag_negation: bool = True
    flag_quotes: bool = True

    @field_validator('name', 'topic')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():raise ValueError('Blank name/topic')
        return value.strip()

    @field_validator('groups', 'context_any', 'exclude_any')
    @classmethod
    def terms(cls, value, info):
        groups=value if info.field_name=='groups' else [value]
        for group in groups:
            if info.field_name=='groups' and not 1<=len(group)<=30:raise ValueError('Each synonym group needs 1..30 terms')
            if any(not isinstance(term,str) or not term.strip() or len(term)>80 or any(ord(c)<32 for c in term) for term in group):
                raise ValueError('Terms need 1..80 printable characters')
            if len({normalize(t.strip()) for t in group})!=len(group):raise ValueError('Duplicate normalized term')
        return [[t.strip() for t in g] for g in value] if info.field_name=='groups' else [t.strip() for t in value]

    def digest(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @model_validator(mode='after')
    def term_budget(self):
        if sum(map(len,self.groups))+len(self.context_any)+len(self.exclude_any)>120:
            raise ValueError('At most 120 terms across the rule')
        return self


def quote_spans(text):
    result=[];start=None;closer=None
    for index,char in enumerate(text):
        if closer is not None:
            if char==closer:result.append((start,index+1));start=None;closer=None
            elif char=='\n' and closer=='"':start=None;closer=None
        elif char in ('“','"','「'):
            start=index;closer={'“':'”','"':'"','「':'」'}[char]
    return result


def occurrences(term, text):
    term=normalize(term)
    pattern=re.escape(term)
    if re.fullmatch(r'[a-z0-9_ ]+',term):pattern=r'(?<![a-z0-9_])'+pattern+r'(?![a-z0-9_])'
    return re.finditer(pattern,text)


def evaluate(rule:Rule, body:str, person_id=None):
    base={'engine_version':ENGINE_VERSION,'semantic_relevance':'not_assessed','topic':rule.topic,
          'severity':rule.severity,'notification_sent':False,'hits':[]}
    if rule.person_ids and str(person_id) not in {str(p) for p in rule.person_ids}:return {**base,'status':'out_of_scope'}
    if not body or len(body)>100000:return {**base,'status':'unassessed','reason':'missing_or_overlong_main_text'}
    text=normalize(body)
    quotes=quote_spans(text);quote_starts=[a for a,b in quotes]
    hits=[];found=set();total=0;review=False
    for index,group in enumerate(rule.groups):
        for term in group:
            for match in occurrences(term,text):
                start,end=match.span();near=text[max(0,start-rule.window):end+rule.window]
                if rule.context_any and not any(next(occurrences(c,near),None) for c in rule.context_any):continue
                if any(next(occurrences(c,near),None) for c in rule.exclude_any):continue
                flags=[]
                if rule.flag_negation and re.search(r'\b(no|not|never|deny|denies|without)\b|不|未|没有|否认',text[max(0,start-60):end+30]):flags.append('possible_negation')
                quote_index=bisect_right(quote_starts,start)-1
                if rule.flag_quotes and quote_index>=0 and end<=quotes[quote_index][1]:flags.append('quoted_text')
                review=review or bool(flags);found.add(index);total+=1
                if len(hits)<20:hits.append({'term':term,'group':index,'normalized_start':start,'normalized_end':end,
                    'normalized_excerpt':text[max(0,start-60):end+60],'flags':flags})
    matched=bool(found) and (rule.match_mode=='any' or len(found)==len(rule.groups))
    return {**base,'status':('needs_review' if review else 'matched') if matched else 'no_keyword_match',
            'hits':hits,'hits_omitted':max(0,total-20),'matched_groups':sorted(found),
            'note':'Literal hints only; unmatched text remains available for semantic analysis.'}
