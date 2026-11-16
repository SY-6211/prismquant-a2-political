import hashlib
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from app.local_embedding import chunks, normalized, DIMENSIONS
from app.knowledge import rank
from app.core.config import Settings
from app.main import create_app


class KnowledgeTests(unittest.TestCase):
    def test_chunks_cover_exact_unicode_source_without_truncation(self):
        body = ('Alpha 😀政策。 Interest rates and tariffs.\n\n' * 130) + 'End!'
        parts = chunks(body, lambda value: len(value) + 4)
        end = 0
        for p in parts:
            self.assertLessEqual(p['start'], end)
            self.assertGreater(p['end'], end)
            self.assertEqual(p['text'], body[p['start']:p['end']])
            self.assertEqual(p['text_sha256'], hashlib.sha256(p['text'].encode()).hexdigest())
            self.assertLessEqual(p['tokens'], 384)
            end = p['end']
        self.assertEqual(parts[0]['start'], 0)
        self.assertEqual(end, len(body))
        self.assertEqual(parts, chunks(body, lambda value: len(value) + 4))

    def test_empty_oversized_and_unsplittable_text_rejected(self):
        for body in ('', '   ', 'a' * 100001):
            with self.assertRaises(ValueError): chunks(body, len)
        with self.assertRaises(ValueError): chunks('a', lambda _: 900)

    def test_vector_finite_dimensions_and_normalization(self):
        good = [1.] + [0.] * (DIMENSIONS - 1)
        self.assertEqual(normalized(good), good)
        for bad in (good[:-1], [0.] * DIMENSIONS, [float('nan')] * DIMENSIONS, [float('inf')] * DIMENSIONS, ['1'] * DIMENSIONS):
            with self.assertRaises(ValueError): normalized(bad)

    def test_exact_rank_has_stable_ties_and_does_not_return_vectors(self):
        a = [1.] + [0.] * 383; b = [0., 1.] + [0.] * 382
        rows = [{'id': i, 'embedding': vector} for i, vector in enumerate((b, a, a))]
        self.assertEqual([r['id'] for r in rank(rows, a, 3)], [1, 2, 0])
        self.assertNotIn('embedding', rank(rows, a, 1)[0])

    def test_api_bounds_redacts_raw_question_and_blocks_cross_origin(self):
        with TestClient(create_app(Settings(_env_file=None, environment='test'))) as client, patch('app.api.knowledge.knowledge.search') as search:
            query = {'person_id': str(uuid4()), 'query': 'private test question'}
            for changes in ({'query': 'x' * 501}, {'person_id': 'bad'}, {'query': '  '}, {'until': '2026-01-01'}, {'extra': 'bad'}):
                r = client.post('/api/v1/knowledge/search', json={**query, **changes})
                self.assertEqual(r.status_code, 422)
                self.assertNotIn('private test question', r.text)
                self.assertEqual(r.headers['cache-control'], 'no-store')
            self.assertEqual(client.post('/api/v1/knowledge/search', json=query, headers={'Origin': 'https://other.invalid'}).status_code, 403)
            search.assert_not_called()
            search.return_value = {'items': [], 'paid_api_calls': 0}
            self.assertEqual(client.post('/api/v1/knowledge/search', json=query, headers={'Origin': 'http://testserver'}).status_code, 200)


if __name__ == '__main__': unittest.main()
