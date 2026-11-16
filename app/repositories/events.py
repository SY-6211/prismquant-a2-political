"""Transactional native-event store and bounded read-only queries."""
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from app.core.database import create_read_engine
from app.event_model import EventSnapshot, NAMESPACE, Transport, event_id_for, native_identity
from pydantic import TypeAdapter
from app.repositories.content_stream import lock_clock, append_change


def save_snapshot(connection, source_id: UUID, transport: Transport, snapshot: EventSnapshot,
                  observed_at: datetime, *, record_id: UUID | None = None, link_record=True,
                  observation_kind='live_fetch', metadata: dict | None = None) -> dict:
    """Caller owns transaction. A failed snapshot leaves no partial event writes."""
    TypeAdapter(Transport).validate_python(transport)
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError('Observation time must include a timezone')
    if observation_kind not in ('live_fetch', 'legacy_projection'):
        raise ValueError('Invalid observation kind')
    snapshot = EventSnapshot.model_validate(snapshot)
    platform, native_id = native_identity(snapshot, record_id)
    event_id = event_id_for(platform, native_id)
    channel_id = uuid5(NAMESPACE, f'channel:{source_id}:{transport}')
    digest = snapshot.digest()
    with connection.begin_nested():
        if observation_kind == 'live_fetch':
            lock_clock(connection)
        source = connection.execute(text('SELECT provenance_type FROM sources WHERE source_id=:id FOR SHARE'),
                                    {'id': source_id}).scalar_one_or_none()
        if source != snapshot.provenance_type:
            raise ValueError('Channel publisher provenance disagrees with snapshot')
        # Serialize only this native event across channels/processes. The DB's
        # unique constraints remain the final guard, including channel creation.
        lock_key = int.from_bytes(event_id.bytes[:8], 'big', signed=True)
        connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
        previous_hash = connection.execute(text('SELECT v.snapshot_hash FROM event_observations o '
            'JOIN event_versions v ON v.version_id=o.version_id WHERE o.event_id=:event '
            'ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1'), {'event': event_id}).scalar_one_or_none()
        connection.execute(text('INSERT INTO source_channels (channel_id, source_id, transport) '
                                'VALUES (:id, :source, :transport) ON CONFLICT DO NOTHING'),
                           {'id': channel_id, 'source': source_id, 'transport': transport})
        connection.execute(text('INSERT INTO monitor_events (event_id, platform, native_id) '
                                'VALUES (:id, :platform, :native) ON CONFLICT DO NOTHING'),
                           {'id': event_id, 'platform': platform, 'native': native_id})
        version = connection.execute(text('SELECT version_id, version_number FROM event_versions '
            'WHERE event_id=:event AND channel_id=:channel AND snapshot_hash=:hash'),
            {'event': event_id, 'channel': channel_id, 'hash': digest}).mappings().one_or_none()
        created = version is None
        if created:
            number = connection.execute(text('SELECT coalesce(max(version_number), 0)+1 FROM event_versions '
                                              'WHERE event_id=:id'), {'id': event_id}).scalar_one()
            version = {'version_id': uuid4(), 'version_number': number}
            connection.execute(text('INSERT INTO event_versions '
                '(version_id, event_id, channel_id, version_number, person_id, provenance_type, '
                'verification_status, review_status, payload, snapshot_hash) VALUES '
                '(:version_id, :event, :channel, :version_number, :person, :provenance, :verification, '
                ':review, CAST(:payload AS jsonb), :hash)'),
                {**version, 'event': event_id, 'channel': channel_id, 'person': snapshot.person_id,
                 'provenance': snapshot.provenance_type, 'verification': snapshot.verification_status,
                 'review': snapshot.review_status, 'payload': snapshot.model_dump_json(), 'hash': digest})
        connection.execute(text('INSERT INTO event_observations '
            '(event_id, version_id, observed_at, observation_kind, metadata) VALUES '
            '(:event, :version, :at, :kind, CAST(:metadata AS jsonb))'),
            {'event': event_id, 'version': version['version_id'], 'at': observed_at.astimezone(timezone.utc),
             'kind': observation_kind, 'metadata': json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False, default=str)})
        if record_id is not None and link_record:
            source_match = connection.execute(text('SELECT source_id FROM public_records WHERE record_id=:id'),
                                              {'id': record_id}).scalar_one_or_none()
            if source_match != source_id:
                raise ValueError('Legacy record belongs to another source')
            connection.execute(text('INSERT INTO event_record_links (record_id, event_id, version_id) '
                                    'VALUES (:record, :event, :version) ON CONFLICT DO NOTHING'),
                               {'record': record_id, 'event': event_id, 'version': version['version_id']})
            linked = connection.execute(text('SELECT event_id FROM event_record_links WHERE record_id=:id'),
                                        {'id': record_id}).scalar_one()
            if linked != event_id:
                raise ValueError('Legacy record was already linked to another native event')
        if observation_kind == 'live_fetch' and previous_hash != digest:
            append_change(connection, event_id=event_id, version_id=version['version_id'], record_id=record_id,
                          person_id=snapshot.person_id, kind='new' if previous_hash is None else 'revision',
                          review_status=snapshot.review_status)
    return {'event_id': str(event_id), 'version_id': str(version['version_id']),
            'version_number': version['version_number'], 'version_created': created}


LATEST = """FROM monitor_events e JOIN LATERAL (
    SELECT o.version_id, o.observed_at, o.observation_id FROM event_observations o
    WHERE o.event_id=e.event_id ORDER BY o.observed_at DESC, o.observation_id DESC LIMIT 1
) latest ON true JOIN event_versions v ON v.version_id=latest.version_id
JOIN source_channels c ON c.channel_id=v.channel_id JOIN sources s ON s.source_id=c.source_id
LEFT JOIN people p ON p.person_id=v.person_id """
SUMMARY = """e.event_id, e.platform, e.native_id, v.version_id, v.version_number,
    c.channel_id, c.source_id, c.transport, s.name AS source_name, s.publisher_name,
    v.person_id, p.name_original AS person_name_original, p.name_zh AS person_name_zh,
    v.provenance_type, v.verification_status, v.review_status,
    v.payload->>'title' AS title, v.payload->>'post_type' AS post_type,
    v.payload->>'published_at' AS published_at, latest.observed_at AS last_observed_at """


def select_events(connection, limit=20, cursor=None, person_id=None):
    filters, values = [], {'limit': limit + 1}
    if cursor is not None:
        filters.append('e.event_id > :cursor')
        values['cursor'] = cursor
    if person_id is not None:
        filters.append('v.person_id=:person')
        values['person'] = person_id
    where = ' WHERE ' + ' AND '.join(filters) if filters else ''
    return [dict(row) for row in connection.execute(text('SELECT ' + SUMMARY + LATEST + where +
                ' ORDER BY e.event_id LIMIT :limit'), values).mappings()]


def select_event(connection, event_id):
    row = connection.execute(text('SELECT ' + SUMMARY + ', v.payload, v.snapshot_hash ' + LATEST +
                                  ' WHERE e.event_id=:id'), {'id': event_id}).mappings().one_or_none()
    if row is None:
        return None
    result = dict(row)
    result['linked_record_ids'] = list(connection.execute(text('SELECT record_id FROM event_record_links '
                                                             'WHERE event_id=:id ORDER BY record_id'), {'id': event_id}).scalars())
    return result


def select_versions(connection, event_id, limit=20, before=None):
    # Exclude full payloads from history pages; request one version for its body.
    return [dict(row) for row in connection.execute(text('SELECT v.version_id, v.version_number, '
        'v.channel_id, c.source_id, c.transport, v.person_id, v.provenance_type, v.verification_status, '
        'v.review_status, v.snapshot_hash, v.created_at FROM event_versions v '
        'JOIN source_channels c ON c.channel_id=v.channel_id '
        'WHERE v.event_id=:id AND (CAST(:before AS integer) IS NULL OR v.version_number < :before) '
        'ORDER BY v.version_number DESC LIMIT :limit'), {'id': event_id, 'before': before, 'limit': limit + 1}).mappings()]


def select_version(connection, event_id, version_id):
    row = connection.execute(text('SELECT v.version_id, v.version_number, v.channel_id, c.source_id, c.transport, '
        'v.person_id, v.provenance_type, v.verification_status, v.review_status, v.snapshot_hash, '
        'v.created_at, v.payload FROM event_versions v JOIN source_channels c ON c.channel_id=v.channel_id '
        'WHERE v.event_id=:event AND v.version_id=:version'), {'event': event_id, 'version': version_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def read_query(settings, selector, *args):
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            return selector(connection, *args)
    finally:
        engine.dispose()
