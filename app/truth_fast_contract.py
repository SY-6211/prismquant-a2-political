"""Independently implemented offline decoder for the published 1322 Truth contract.

Reference checked 2026-11-07: https://1322.io/docs (TruthSocialPost section).
This module does not create a WebSocket, fetch media, write SQL or call a model.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.event_model import event_id_for, public_url
from app.preview_trump import PERSON_ID

ADAPTER_VERSION = 'truth1322_offline_v1'
HANDLE = 'realDonaldTrump'
MAX_FRAME_BYTES = 256 * 1024


def reference_url(value: str | None) -> str | None:
    if not value:
        return None
    public_url(value)
    # Query credentials must not escape into replay artifacts. Media retrieval
    # with signed URLs needs its own reviewed policy in a future adapter.
    names = {key.casefold() for key, _ in parse_qsl(urlsplit(value).query)}
    if names & {'key','api_key','apikey','token','access_token','authorization','signature'}:
        raise ValueError('Credential-bearing reference URL is not accepted')
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class Author(ContractModel):
    id: str | None = Field(default=None, pattern=r'^[1-9][0-9]{0,29}$')
    username: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.-]{1,100}$')
    display_name: str | None = Field(default=None, max_length=500)
    avatar: str | None = None

    @field_validator('avatar')
    @classmethod
    def safe_url(cls, value):
        return reference_url(value)


class Quoted(ContractModel):
    key: str = Field(pattern=r'^[1-9][0-9]{0,29}$')
    text: str | None = Field(default=None, max_length=50000)
    url: str | None = None
    author: Author | None = None
    quoted: 'Quoted | None' = None

    @field_validator('url')
    @classmethod
    def safe_url(cls, value):
        return reference_url(value)


class Media(ContractModel):
    kind: str = Field(min_length=1, max_length=40)
    url: str
    poster_url: str | None = None
    created_at: AwareDatetime | None = None
    source: Literal['own','quoted','nested_quoted'] | None = None

    @field_validator('url','poster_url')
    @classmethod
    def safe_url(cls, value, info):
        if info.field_name == 'url' and not value:
            raise ValueError('Media reference URL is required')
        return reference_url(value)

    @field_validator('created_at', mode='before')
    @classmethod
    def unknown_time(cls, value):
        return value or None

    @field_validator('created_at')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if value else None


class LinkCard(ContractModel):
    url: str
    title: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=10000)
    image: str | None = None

    @field_validator('url','image')
    @classmethod
    def safe_url(cls, value, info):
        if info.field_name == 'url' and not value:
            raise ValueError('Link reference URL is required')
        return reference_url(value)


class ProviderFrame(BaseModel):
    model_config = ConfigDict(extra='allow', hide_input_in_errors=True)
    platform: Literal['truth']
    username: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}$')
    display_name: str | None = Field(default=None, max_length=500)
    user_id: str = Field(pattern=r'^[1-9][0-9]{0,29}$')
    key: str = Field(pattern=r'^[1-9][0-9]{0,29}$')
    timestamp: AwareDatetime | None = None
    seen_at: AwareDatetime | None = None
    text: str | None = Field(default=None, max_length=50000)
    is_quote: StrictBool = False
    is_retruth: StrictBool = False
    quoted: Quoted | None = None
    retruth_of_id: str | None = Field(default=None, pattern=r'^[1-9][0-9]{0,29}$')
    retruth_of: Author | None = None
    media: list[Media] = Field(default_factory=list, max_length=50)
    card: LinkCard | None = None

    @field_validator('timestamp','seen_at', mode='before')
    @classmethod
    def unknown_time(cls, value):
        return value or None

    @field_validator('timestamp','seen_at')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if value else None

    @model_validator(mode='after')
    def nesting_and_ownership(self):
        if self.quoted and not (self.is_quote or self.is_retruth):
            raise ValueError('Quoted content without an explicit quote/retruth flag')
        if self.is_quote and not self.quoted:
            raise ValueError('Quote flag without quoted content')
        if self.is_retruth and not self.retruth_of_id:
            raise ValueError('Retruth reference is missing')
        if not self.is_retruth and (self.retruth_of_id or self.retruth_of):
            raise ValueError('Unexpected retruth attribution')
        current, depth = self.quoted, 0
        while current:
            depth += 1
            if depth > 2:
                raise ValueError('Quote depth exceeds the documented contract')
            if current.url:
                parts = urlsplit(current.url)
                if parts.hostname not in ('truthsocial.com','www.truthsocial.com') or parts.path.rstrip('/').split('/')[-1] != current.key:
                    raise ValueError('Quoted post address and native ID disagree')
            current = current.quoted
        if not (self.text and self.text.strip()) and not (self.quoted or self.media):
            raise ValueError('No content in provider frame')
        return self


class QuotePart(ContractModel):
    role: Literal['retruthed_post','quoted_post']
    depth: int
    native_id: str
    text: str | None
    author: Author | None
    url: str | None


class NormalizedEvent(ContractModel):
    adapter_version: str = ADAPTER_VERSION
    provider: Literal['1322'] = '1322'
    platform: Literal['truth_social'] = 'truth_social'
    event_id: UUID
    native_id: str
    person_id: UUID = PERSON_ID
    reported_user_id: str
    reported_handle: str
    reported_display_name: str | None
    post_type: Literal['original','quote','retruth']
    original_url: str
    main_text: str | None
    quoted_content: list[QuotePart]
    media_references: list[Media]
    link_card: LinkCard | None
    reported_published_at: AwareDatetime | None
    provider_detected_at: AwareDatetime | None
    exported_received_at: AwareDatetime
    source_identity: Literal['independent_third_party_candidate'] = 'independent_third_party_candidate'
    verification_status: Literal['provider_reported_unverified'] = 'provider_reported_unverified'
    eligible_under_existing_content_scope: bool
    unknown_field_count: int
    time_warnings: list[str]
    publication_latency_verified: Literal[False] = False
    database_saved: Literal[False] = False

    def content_hash(self):
        value = self.model_dump(mode='json', exclude={'provider_detected_at','exported_received_at','time_warnings'})
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def normalize_frame(raw: dict, expected_user_id: str, received_at: datetime) -> NormalizedEvent:
    import re
    if not re.fullmatch(r'[1-9][0-9]{0,29}', expected_user_id) or received_at.utcoffset() is None:
        raise ValueError('An explicit account ID and timezone-aware receipt time are required')
    frame = ProviderFrame.model_validate(raw)
    if frame.user_id != expected_user_id or frame.username.casefold() != HANDLE.casefold():
        raise ValueError('identity_mismatch')
    post_type = 'retruth' if frame.is_retruth else 'quote' if frame.is_quote else 'original'
    quotes = []
    if frame.is_retruth:
        author = frame.retruth_of
        url = f'https://truthsocial.com/@{author.username}/{frame.retruth_of_id}' if author and author.username else None
        quotes.append(QuotePart(role='retruthed_post',depth=0,native_id=frame.retruth_of_id,text=frame.text,author=author,url=url))
    current, depth = frame.quoted, 1
    while current:
        quotes.append(QuotePart(role='quoted_post',depth=depth,native_id=current.key,text=current.text,author=current.author,url=current.url))
        current, depth = current.quoted, depth+1
    ignored_metadata = {'user_avatar','user_following','user_followers'}
    unknown = len(set(frame.model_extra or {}) - ignored_metadata)
    main = None if frame.is_retruth else (frame.text if frame.text and frame.text.strip() else None)
    times = []
    if frame.timestamp and frame.seen_at and frame.seen_at < frame.timestamp:
        times.append('provider_clock_precedes_reported_publication')
    if frame.seen_at and received_at < frame.seen_at:
        times.append('receipt_clock_precedes_provider_detection')
    return NormalizedEvent(
        event_id=event_id_for('truth_social',frame.key),native_id=frame.key,reported_user_id=frame.user_id,
        reported_handle=frame.username,reported_display_name=frame.display_name,post_type=post_type,
        original_url=f'https://truthsocial.com/@{HANDLE}/{frame.key}',main_text=main,quoted_content=quotes,
        media_references=frame.media,link_card=frame.card,reported_published_at=frame.timestamp,
        provider_detected_at=frame.seen_at,exported_received_at=received_at.astimezone(timezone.utc),
        eligible_under_existing_content_scope=bool(post_type=='original' and main and not unknown),
        unknown_field_count=unknown,time_warnings=times,
    )
