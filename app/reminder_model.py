"""No model calls: literal hints retain the original event and rule versions."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,field_validator
from app.keyword_rules import normalize
import hashlib,re

Status=Literal['active','pending_review','grouped','duplicate','superseded','corrected']
Priority=Literal['low','medium','high']
def body_hash(body):return hashlib.sha256(re.sub(r'\s+',' ',normalize(body)).strip().encode()).hexdigest()

class Feedback(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    reminder_id:UUID
    client_request_id:UUID
    verdict:Literal['false_positive','useful']
    note:str=Field(min_length=1,max_length=500)
    @field_validator('note')
    @classmethod
    def printable(cls,value):
        if not value.strip() or any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError('Invalid note')
        return value.strip()
