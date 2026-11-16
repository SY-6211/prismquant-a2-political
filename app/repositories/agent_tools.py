"""Fixed, bound, read-only queries for a single Agent retrieval snapshot."""
from sqlalchemy import text


class SnapshotReader:
    def __init__(self, connection):
        self.connection = connection
        boundary = connection.execute(text('SELECT clock_timestamp() AS captured_at, '
            'coalesce(max(observation_id),0) AS observation_head FROM event_observations')).mappings().one()
        self.captured_at = boundary['captured_at']
        self.head = boundary['observation_head']

    def people(self, people, query):
        return [dict(r) for r in self.connection.execute(text(
            'SELECT person_id,left(name_original,200) AS name_original,left(name_zh,200) AS name_zh '
            'FROM people WHERE person_id=ANY(CAST(:people AS uuid[])) '
            "AND (:q='' OR strpos(lower(name_original),lower(:q))>0 OR strpos(lower(coalesce(name_zh,'')),lower(:q))>0) "
            'ORDER BY person_id LIMIT 20'), {'people': list(people), 'q': query}).mappings()]

    def is_reserved(self,source):
        # Analysis preparation may check holdout membership, never labels/content.
        return bool(self.connection.execute(text("SELECT EXISTS (SELECT 1 FROM annotation_samples "
            "WHERE split='blind' AND origin='source' AND (event_key=:event OR snapshot->>'event_version_id'=:version))"),
            {'event':str(source['event_id']),'version':str(source['version_id'])}).scalar_one())

    def hybrid(self,args):
        from app.rag import retrieve
        return retrieve(self,args)

    def research_version_eligible(self,people,args):
        return bool(self.connection.execute(text("""SELECT EXISTS(SELECT 1 FROM event_versions v
          WHERE v.event_id=:event AND v.version_id=:version AND v.person_id=ANY(CAST(:people AS uuid[]))
          AND v.review_status='accepted' AND v.payload->>'post_type' IN ('original','speech')
          AND v.payload->>'content_type' IN ('public_statement','institution_announcement')
          AND char_length(coalesce(v.payload->>'main_text','')) BETWEEN 1 AND 100000
          AND EXISTS(SELECT 1 FROM event_observations o WHERE o.version_id=v.version_id AND o.observation_id<=:head)
          AND (CAST(:since AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz>=:since)
          AND (CAST(:until AS timestamptz) IS NULL OR (v.payload->>'published_at')::timestamptz<:until)
          AND NOT EXISTS(SELECT 1 FROM annotation_samples a WHERE a.split='blind' AND a.origin='source'
            AND (a.event_key=v.event_id::text OR a.snapshot->>'event_version_id'=v.version_id::text)))"""),
          {'event':args.event_id,'version':args.version_id,'people':list(people),'head':self.head,'since':args.since,'until':args.until}).scalar_one())

    def search(self, people, args):
        return [dict(r) for r in self.connection.execute(text(
            'SELECT e.event_id,v.version_id,v.version_number,v.person_id,v.provenance_type,v.verification_status,'
            'v.review_status,left(v.payload->>\'title\',300) AS title,v.payload->>\'published_at\' AS published_at,'
            'o.observed_at AS last_observed_at,left(v.payload->>\'main_text\',220) AS main_text_preview '
            'FROM monitor_events e JOIN LATERAL (SELECT version_id,observed_at FROM event_observations '
            'WHERE event_id=e.event_id AND observation_id<=:head ORDER BY observed_at DESC,observation_id DESC LIMIT 1) o ON true '
            'JOIN event_versions v ON v.version_id=o.version_id '
            'WHERE v.person_id=ANY(CAST(:people AS uuid[])) AND v.person_id=:person '
            "AND (:q='' OR strpos(lower(v.payload->>'title'),lower(:q))>0 OR strpos(lower(coalesce(v.payload->>'main_text','')),lower(:q))>0) "
            "AND (CAST(:since AS timestamptz) IS NULL OR CAST(v.payload->>'published_at' AS timestamptz)>=:since) "
            "AND (CAST(:until AS timestamptz) IS NULL OR CAST(v.payload->>'published_at' AS timestamptz)<:until) "
            'ORDER BY o.observed_at DESC,e.event_id DESC LIMIT :limit OFFSET :offset'),
            {'people': list(people), 'person': args.person_id, 'q': args.query, 'head': self.head,
             'since': args.since, 'until': args.until, 'limit': args.limit + 1, 'offset': args.offset}).mappings()]

    def versions(self, people, args):
        return [dict(r) for r in self.connection.execute(text(
            'SELECT v.event_id,v.version_id,v.version_number,v.person_id,v.provenance_type,v.verification_status,'
            "v.review_status,v.snapshot_hash,v.created_at,char_length(v.payload->>'main_text') AS main_text_characters FROM event_versions v "
            'WHERE v.event_id=:event AND v.person_id=ANY(CAST(:people AS uuid[])) '
            'AND (CAST(:before AS integer) IS NULL OR v.version_number<:before) '
            'AND EXISTS (SELECT 1 FROM event_observations o WHERE o.version_id=v.version_id AND o.observation_id<=:head) '
            'ORDER BY v.version_number DESC LIMIT :limit'),
            {'event': args.event_id, 'people': list(people), 'head': self.head,
             'before': args.before, 'limit': args.limit + 1}).mappings()]

    def segment(self, people, args):
        # The full body/metadata are never transferred from PostgreSQL. SQL slices
        # Unicode characters; offsets returned to Python remain zero-based.
        row = self.connection.execute(text(
            'SELECT v.event_id,v.version_id,v.version_number,v.person_id,v.snapshot_hash,v.created_at AS version_created_at,'
            '(SELECT max(o.observed_at) FROM event_observations o WHERE o.version_id=v.version_id '
            'AND o.observation_id<=:head) AS version_last_observed_at,'
            'v.provenance_type,v.verification_status,v.review_status,c.transport,c.source_id,'
            'left(s.name,200) AS source_name,left(s.publisher_name,200) AS publisher_name,'
            "left(v.payload->>'title',300) AS title,v.payload->>'published_at' AS published_at,"
            "CASE WHEN length(v.payload->>'original_url')<=2048 THEN v.payload->>'original_url' END AS original_url,"
            "CASE WHEN length(v.payload->>'retrieved_url')<=2048 THEN v.payload->>'retrieved_url' END AS retrieved_url,"
            "char_length(part.value->>'text') AS total_characters,"
            "substring(part.value->>'text' FROM :start+1 FOR :length) AS text,"
            "left(part.value->>'kind',80) AS part_kind,left(part.value->>'reported_author',200) AS reported_author,"
            "left(part.value->>'association_status',80) AS association_status "
            'FROM event_versions v JOIN source_channels c ON c.channel_id=v.channel_id '
            'JOIN sources s ON s.source_id=c.source_id CROSS JOIN LATERAL (SELECT CASE '
            "WHEN :section='main_text' THEN jsonb_build_object('text',v.payload->>'main_text','kind','main_text',"
            "'reported_author',v.payload->>'reported_author') "
            "WHEN :section='quoted_content' THEN v.payload->'quoted_content'->CAST(:position AS integer) "
            "ELSE v.payload->'media_notes'->CAST(:position AS integer) END AS value) part "
            'WHERE v.event_id=:event AND v.version_id=:version AND v.person_id=ANY(CAST(:people AS uuid[])) '
            'AND EXISTS (SELECT 1 FROM event_observations o WHERE o.version_id=v.version_id AND o.observation_id<=:head)'),
            {'event': args.event_id, 'version': args.version_id, 'people': list(people), 'head': self.head,
             'section': args.section, 'position': args.position, 'start': args.start, 'length': args.length}).mappings().one_or_none()
        return dict(row) if row is not None else None
