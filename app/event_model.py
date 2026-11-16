"""Typed event snapshots. Transport, attribution and quoted text are independent."""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

NAMESPACE = UUID('2d6dc050-1c0c-4d4f-b845-5780026134d0')
Transport = Literal['rss', 'web_index', 'official_api', 'licensed_stream', 'manual_import']
Provenance = Literal['official', 'third_party_archive']
Verification = Literal['verified_page_attribution', 'archive_only', 'media_quote_only', 'needs_review']


def public_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in ('https', 'http') or not parts.hostname or parts.username is not None
            or parts.password is not None or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('Expected a public HTTP URL without credentials')
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', revalidate_instances='always')


class QuotedContent(StrictModel):
    kind: Literal['inline_quote', 'quoted_post']
    position: int = Field(ge=0)
    text: str | None
    reported_author: str | None = None
    original_url: str | None = None
    # Original extracted fields are retained, including unknown attribution.
    metadata: dict = Field(default_factory=dict)


class MediaContent(StrictModel):
    position: int = Field(ge=0)
    kind: Literal['archive_image_description', 'archive_transcript', 'unavailable', 'other_archive_note']
    label: str | None = None
    text: str | None = None
    attachment_ref: str | None = None
    owner_archive_url: str | None = None
    association_status: str = 'unresolved'
    attribution: Literal['archive_supplied_not_main_text'] = 'archive_supplied_not_main_text'


class EventSnapshot(StrictModel):
    title: str = Field(min_length=1)
    content_type: Literal['public_statement', 'institution_announcement', 'media_quote', 'unconfirmed']
    post_type: Literal['speech', 'original', 'quote', 'retruth', 'unknown']
    main_text: str | None
    original_url: str | None
    retrieved_url: str
    published_at: AwareDatetime | None
    page_date: date | None
    published_raw: str | None
    language: str | None
    person_id: UUID | None
    reported_author: str | None = None
    reported_handle: str | None = None
    speaker_evidence: str | None
    provenance_type: Provenance
    verification_status: Verification
    review_status: Literal['accepted', 'needs_review'] = 'accepted'
    quoted_content: list[QuotedContent] = Field(default_factory=list)
    media_notes: list[MediaContent] = Field(default_factory=list)
    image_count: int = Field(default=0, ge=0)
    video_count: int = Field(default=0, ge=0)
    parser_version: str
    archive_removed_raw: str | None = None

    @field_validator('original_url', 'retrieved_url')
    @classmethod
    def valid_url(cls, value):
        return public_url(value) if value is not None else None

    @field_validator('published_at')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if value else None

    @model_validator(mode='after')
    def attribution_and_content(self):
        if not self.title.strip() or not self.parser_version.strip():
            raise ValueError('Nonblank title and parser version required')
        if self.main_text is not None and not self.main_text.strip():
            raise ValueError('Use null for missing main text')
        if self.verification_status == 'verified_page_attribution':
            if self.person_id is None or not self.speaker_evidence or not self.speaker_evidence.strip():
                raise ValueError('Page attribution requires a person and evidence')
            if self.provenance_type != 'official':
                raise ValueError('Third-party snapshots cannot be promoted to verified official pages')
        if self.main_text is None and not (self.quoted_content or self.media_notes or self.image_count or self.video_count):
            raise ValueError('Empty events need review outside the event store')
        if self.post_type == 'retruth' and self.main_text is not None:
            raise ValueError('A repost must store its text as quoted content')
        return self

    def digest(self) -> str:
        data = json.dumps(self.model_dump(mode='json'), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(data.encode('utf-8')).hexdigest()


def native_identity(snapshot: EventSnapshot, record_id: UUID | None = None) -> tuple[str, str]:
    """Use provider IDs, never titles or archive-local IDs as original-post IDs."""
    url = snapshot.original_url or snapshot.retrieved_url
    parts = urlsplit(url)
    if parts.scheme == 'https' and parts.netloc.lower() in ('truthsocial.com', 'www.truthsocial.com'):
        match = re.fullmatch(r'/@[A-Za-z0-9_.-]+/(?:posts/)?([0-9]+)', parts.path)
        if match and not parts.query:
            return 'truth_social', match[1]
    if parts.scheme == 'https' and parts.netloc.lower() == 'www.ecb.europa.eu':
        # Reuse the collector's strict URL normalizer, including duplicate slashes.
        from app.preview_ecb_speech import speech_url
        try:
            return 'ecb', urlsplit(speech_url(url)).path
        except ValueError:
            pass
    if record_id is not None:
        return 'source_record', str(record_id)
    raise ValueError('No supported native identity; do not guess a cross-source match')


def event_id_for(platform: str, native_id: str) -> UUID:
    return uuid5(NAMESPACE, json.dumps([platform, native_id], separators=(',', ':')))


def snapshot_from_record(record: dict, archive: dict | None = None, *, review_status='accepted') -> EventSnapshot:
    archive = archive or {}
    quotes = [QuotedContent(kind='inline_quote', position=q['position'], text=q.get('text'),
                            reported_author=q.get('attribution'), metadata=q) for q in archive.get('inline_quotes', [])]
    quotes.extend(QuotedContent(kind='quoted_post', position=q['position'], text=q.get('text'),
                                reported_author=q.get('archive_author'), original_url=q.get('original_url'), metadata=q)
                  for q in archive.get('quoted_posts', []))
    return EventSnapshot(
        **{key: record.get(key) for key in ('title', 'content_type', 'original_url', 'retrieved_url',
           'published_at', 'page_date', 'published_raw', 'language', 'person_id', 'speaker_evidence',
           'provenance_type', 'verification_status')},
        main_text=record.get('body_text') or None,
        post_type=archive.get('archive_post_type', 'speech' if record['provenance_type'] == 'official' else 'unknown'),
        reported_author=archive.get('archive_author'), reported_handle=archive.get('archive_handle'),
        quoted_content=quotes, media_notes=[MediaContent(**note) for note in archive.get('archive_media_notes', [])],
        image_count=archive.get('image_count', 0), video_count=archive.get('video_count', 0),
        archive_removed_raw=archive.get('archive_removed_raw'),
        parser_version=archive.get('parser_version', 'ecb_speech_v1'), review_status=review_status,
    )
