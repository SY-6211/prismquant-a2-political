"""Candidate inventory only: a review never enables collection or grants access."""
import ipaddress,re
from typing import Literal
from urllib.parse import urlsplit,parse_qsl
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

Decision=Literal['needs_review','awaiting_access','out_of_scope','pilot_candidate']
def public_url(value):
    if value is None:return None
    if not isinstance(value,str) or len(value)>2048 or any(c.isspace() or ord(c)<32 for c in value):raise ValueError('Invalid public URL')
    parsed=urlsplit(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:raise ValueError('Invalid public URL')
    hostname=parsed.hostname.lower()
    try:
        if not ipaddress.ip_address(hostname).is_global:raise ValueError('Private addresses are not source URLs')
    except ValueError as error:
        if 'Private addresses' in str(error):raise
        if '.' not in hostname or hostname.endswith(('.local','.localhost')):raise ValueError('A public hostname is required') from None
    parsed.port
    if any(re.search(r'token|password|secret|api.?key|authorization|signature|session',k,re.I) or v.startswith(('sk-','pqchat_')) for k,v in parse_qsl(parsed.query)):raise ValueError('Do not put credentials in a catalog URL')
    return value

class Review(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    expected_revision:int=Field(ge=0)
    decision:Decision
    relevance:Literal['person_monitoring','background','out_of_scope']='background'
    discovery_url:str|None=None
    transport:Literal['unselected','rss','api','html','browser','manual']='unselected'
    attribution:Literal['unverified','reviewed','disputed']='unverified'
    person_names:list[str]=Field(default_factory=list,max_length=8)
    access:Literal['unreviewed','public_documented','requires_permission','restricted','unavailable']='unreviewed'
    evidence_urls:list[str]=Field(default_factory=list,max_length=8)
    note:str=Field(min_length=1,max_length=2000)

    @field_validator('discovery_url')
    @classmethod
    def url(cls,value):return public_url(value)
    @field_validator('evidence_urls')
    @classmethod
    def urls(cls,values):return list(dict.fromkeys(public_url(v) for v in values))
    @field_validator('person_names')
    @classmethod
    def names(cls,values):
        if any(not v.strip() or len(v)>120 or any(ord(c)<32 for c in v) for v in values):raise ValueError('Invalid person names')
        return list(dict.fromkeys(v.strip() for v in values))
    @field_validator('note')
    @classmethod
    def nonblank(cls,value):
        if not value.strip() or '\x00' in value:raise ValueError('A review explanation is required')
        return value.strip()
    @model_validator(mode='after')
    def candidate_requirements(self):
        if self.decision=='pilot_candidate' and (not self.discovery_url or self.transport=='unselected' or self.attribution!='reviewed' or self.access!='public_documented' or not self.evidence_urls):raise ValueError('A pilot needs a discovery URL, transport, attribution and documented access evidence')
        if self.relevance=='person_monitoring' and self.decision=='pilot_candidate' and not self.person_names:raise ValueError('Specify the attributed people')
        if self.decision=='out_of_scope' and self.relevance!='out_of_scope':raise ValueError('Out-of-scope decision needs matching relevance')
        return self
