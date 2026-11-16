"""Conversation input contracts; ownership and roles are never client arguments."""
import hashlib
import json
import re
import secrets
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

TOKEN_RE=re.compile(r'^pqchat_[A-Za-z0-9_-]{43}$')


def new_token():return 'pqchat_'+secrets.token_urlsafe(32)


def token_digest(token):
    if not isinstance(token,str) or not TOKEN_RE.fullmatch(token):raise ValueError('Invalid chat credential')
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def creation_hash(title,person_id):
    return hashlib.sha256(json.dumps({'title':title,'person_id':str(person_id) if person_id else None},
        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)


class CreateConversation(Strict):
    client_request_id:UUID
    title:str=Field(min_length=1,max_length=120)
    person_id:UUID|None=None

    @field_validator('title')
    @classmethod
    def clean_title(cls,value):
        value=value.strip()
        if not value or any(ord(c)<32 for c in value):raise ValueError('Nonblank single-line title required')
        return value


class EditConversation(Strict):
    expected_revision:int=Field(ge=1)
    title:str|None=Field(default=None,min_length=1,max_length=120)
    archived:bool|None=None

    @field_validator('title')
    @classmethod
    def clean_title(cls,value):return CreateConversation.clean_title(value) if value is not None else value

    @model_validator(mode='after')
    def edit_present(self):
        if self.title is None and self.archived is None:raise ValueError('A title or archive change is required')
        return self


class UserMessage(Strict):
    client_request_id:UUID
    content:str=Field(min_length=1,max_length=4000)

    @field_validator('content')
    @classmethod
    def nonblank(cls,value):
        if not value.strip() or '\x00' in value:raise ValueError('Nonblank message required')
        return value
