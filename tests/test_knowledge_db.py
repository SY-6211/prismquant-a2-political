"""Vector and version correctness in a disposable PostgreSQL schema only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, datetime, timezone
import json, os, unittest
from unittest.mock import patch
from uuid import UUID, uuid4
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.local_embedding import chunks, PROFILE_ID
from app.repositories import knowledge as repo
from app.knowledge import index_one
import test_agent_tools_db as fixtures

VECTOR = [1.] + [0.] * 383


@unittest.skipUnless(os.environ.get('PQ_KNOWLEDGE_DB_TESTS') == '1', 'Isolated knowledge DB tests are opt-in')
class KnowledgeDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AgentToolDatabaseTests.setUpClass.__func__)
    drop_schema = classmethod(fixtures.AgentToolDatabaseTests.drop_schema.__func__)

    def setUp(self):
        fixtures.AgentToolDatabaseTests.setUp(self)
        self.snap = self.snap.model_copy(update={'post_type': 'original'})
        self.at = self.now + timedelta(seconds=1)
        self.version = self.save()

    def save(self, snap=None, at=None):
        return fixtures.AgentToolDatabaseTests.save(self, snap, at or getattr(self, 'at', self.now))

    def ready(self):
        repo.enqueue(self.c); target = repo.claim(self.c)
        parts = chunks(target['body'], lambda value: len(value) + 4)
        repo.publish(self.c, target, parts, [VECTOR] * len(parts))
        return target, parts

    def test_queue_is_idempotent_and_binds_exact_unicode_offsets(self):
        self.assertEqual(repo.enqueue(self.c)['queued'], 1)
        self.assertEqual(repo.enqueue(self.c)['queued'], 0)
        target, parts = self.ready()
        rows = repo.candidates(self.c, self.person)
        self.assertEqual(rows[0]['text'], target['body'])
        self.assertEqual(rows[0]['version_id'], UUID(self.version['version_id']))
        self.assertEqual(rows[0]['end_offset'], len(target['body']))
        self.assertEqual(repo.status(self.c)['latest_states'], {'ready': 1})
        self.assertEqual(repo.cached(self.c, [parts[0]['text_sha256']])[parts[0]['text_sha256']], VECTOR)

    def test_failed_partial_publish_rolls_back_all_chunks_then_explicit_retry(self):
        repo.enqueue(self.c); target = repo.claim(self.c)
        parts = chunks(target['body'], lambda value: len(value) + 4)
        bad = [{**parts[0], 'end': parts[0]['end'] - 1, 'text': parts[0]['text'][:-1]}]
        with self.assertRaises(ValueError), self.c.begin_nested(): repo.publish(self.c, target, bad, [VECTOR])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM kb_chunks')).scalar_one(), 0)
        repo.fail(self.c, target); self.assertEqual(repo.status(self.c)['latest_states'], {'failed': 1})
        self.assertEqual(repo.enqueue(self.c)['queued'], 0)  # no automatic retries
        self.assertEqual(repo.reset_unsent(self.c, target['version_id'])['reset'], 1)
        replacement = repo.claim(self.c)
        with self.assertRaises(ValueError): repo.publish(self.c, target, parts, [VECTOR])
        self.assertEqual(repo.publish(self.c, replacement, parts, [VECTOR])['state'], 'ready')

    def test_gaps_and_missing_end_never_become_searchable(self):
        repo.enqueue(self.c); target = repo.claim(self.c); body = target['body']
        parts = chunks(body[:-1], lambda value: len(value) + 4)
        with self.assertRaises(ValueError), self.c.begin_nested(): repo.publish(self.c, target, parts, [VECTOR])
        self.assertEqual(repo.candidates(self.c, self.person), [])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM kb_chunks')).scalar_one(), 0)

    def test_latest_revision_excludes_previous_ready_until_new_ready(self):
        self.ready()
        revised = self.snap.model_copy(update={'main_text': 'Revised policy 😀 new statement.'})
        latest = self.save(revised, self.at + timedelta(seconds=1))
        self.assertEqual(repo.candidates(self.c, self.person), [])
        self.ready()
        self.assertEqual(str(repo.candidates(self.c, self.person)[0]['version_id']), latest['version_id'])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM kb_documents')).scalar_one(), 2)
        # A real re-observation of the earlier version makes that exact version current again.
        self.save(self.snap, self.at + timedelta(seconds=2))
        self.assertEqual(str(repo.candidates(self.c, self.person)[0]['version_id']), self.version['version_id'])

    def test_version_superseded_during_inference_is_discarded(self):
        repo.enqueue(self.c); target = repo.claim(self.c)
        self.save(self.snap.model_copy(update={'main_text': 'Newer body'}), self.at + timedelta(seconds=1))
        parts = chunks(target['body'], len)
        self.assertEqual(repo.publish(self.c, target, parts, [VECTOR])['state'], 'skipped')
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM kb_chunks')).scalar_one(), 0)
        self.assertEqual(repo.enqueue(self.c)['queued'], 1)

    def test_person_and_date_scopes_are_enforced_before_ranking(self):
        self.ready()
        self.assertEqual(repo.candidates(self.c, self.other), [])
        self.assertEqual(repo.candidates(self.c, self.person, since=datetime(2030, 1, 1, tzinfo=timezone.utc)), [])
        self.assertEqual(repo.candidates(self.c, self.person, until=datetime(2020, 1, 1, tzinfo=timezone.utc)), [])

    def test_unconfirmed_and_unknown_and_quotes_never_indexed(self):
        for offset, changes in enumerate(({'review_status': 'needs_review'}, {'post_type': 'unknown'}, {'post_type': 'quote'}), 1):
            self.save(self.snap.model_copy(update=changes), self.at + timedelta(seconds=offset))
            self.assertEqual(repo.enqueue(self.c)['queued'], 0)

    def test_main_only_not_media_or_quoted_content(self):
        from app.event_model import MediaContent
        snap = self.snap.model_copy(update={'media_notes': [MediaContent(position=0, kind='archive_transcript', text='DO NOT INDEX MEDIA')]})
        self.save(snap, self.at + timedelta(seconds=1))
        self.ready()
        self.assertNotIn('DO NOT INDEX MEDIA', repo.candidates(self.c, self.person)[0]['text'])

    def test_blind_source_holdout_excluded_without_reading_labels(self):
        dataset = uuid4()
        self.c.execute(text("INSERT INTO annotation_datasets(dataset_id,name) VALUES(:id,'Test reserved')"), {'id': dataset})
        self.c.execute(text("INSERT INTO annotation_samples(sample_id,dataset_id,identity,origin,snapshot,event_key,split) VALUES(:id,:dataset,'reserved','source','{}',:event,'blind')"), {'id': uuid4(), 'dataset': dataset, 'event': self.version['event_id']})
        self.assertEqual(repo.enqueue(self.c)['queued'], 0)
        self.assertEqual(repo.status(self.c)['eligible_latest_versions'], 0)

    def test_database_enforces_vector_integrity_and_append_only_chunks(self):
        target, _ = self.ready()
        for value in ([0.] * 384, [1.], [float('nan')] * 384, [[1.]]):
            self.assertFalse(self.c.execute(text('SELECT pq_kb_unit_vector(CAST(:value AS real[]))'), {'value': value}).scalar_one())
        for sql in ('UPDATE kb_chunks SET text=text', 'DELETE FROM kb_chunks', 'UPDATE kb_profiles SET definition=definition'):
            with self.assertRaises(DBAPIError), self.c.begin_nested(): self.c.execute(text(sql))

    def test_cache_reuses_vectors_but_keeps_separate_source_versions(self):
        _, parts = self.ready()
        other = self.snap.model_copy(update={'person_id': self.other, 'original_url': f'https://truthsocial.com/@test/{uuid4().int}'})
        self.save(other, self.at + timedelta(seconds=1)); repo.enqueue(self.c)
        target = repo.claim(self.c); cached = repo.cached(self.c, [parts[0]['text_sha256']])
        repo.publish(self.c, target, parts, [cached[parts[0]['text_sha256']]])
        a, b = repo.candidates(self.c, self.person)[0], repo.candidates(self.c, self.other)[0]
        self.assertNotEqual(a['chunk_id'], b['chunk_id'])
        self.assertNotEqual(a['event_id'], b['event_id'])
        self.assertEqual(a['text'], b['text'])

    def test_worker_failure_marks_document_failed_without_remote_fallback(self):
        repo.enqueue(self.c)
        def local(settings, fn, *args): return fn(self.c, *args)
        with patch('app.knowledge.write', side_effect=local), patch('app.knowledge.encoder', side_effect=ImportError('Missing local dependency')):
            with self.assertRaises(ImportError): index_one(None)
        self.assertEqual(repo.status(self.c)['latest_states'], {'failed': 1})

    def test_z_concurrent_claims_are_fenced(self):
        repo.enqueue(self.c); self.tx.commit()
        def claim():
            with self.engine.begin() as c: return repo.claim(c)
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)


if __name__ == '__main__': unittest.main()
