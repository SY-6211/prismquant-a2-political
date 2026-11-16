"""Observe collector stages without network traffic or database writes."""

from contextlib import ExitStack
from datetime import datetime, timezone
import importlib
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.timing import elapsed_ms, item_timing, measure_stage


class CollectorTimingTests(unittest.TestCase):
    def run_collector(self, source, *, provided=False, fetch_error=None, list_error=None,
                      save_error=None, guard_error=None):
        module = importlib.import_module('app.import_batch' if source == 'ecb' else 'app.import_trump_batch')
        entry = {'url': 'https://example.invalid/status', 'title': 'Synthetic event',
                 'published_at': '2020-01-01T00:00:00Z', 'published_raw': '2020-01-01T00:00:00Z'}
        selection = {'candidates': [entry], 'selection': {'selected': 1}, 'precheck_results': []}
        context = {'name': 'Synthetic source', 'publisher_name': 'Synthetic publisher',
                   'source_enabled': False, 'monitoring_enabled': False, 'link_enabled': False}
        body = 'Synthetic complete own text. ' * 5
        detail = {'verification_status': 'verified_page_attribution', 'person_id': str(module.PERSON_ID),
                  'body_text': body, 'body_characters': len(body), 'reason': 'Synthetic attribution'}
        saver = MagicMock(side_effect=save_error, return_value={
            'action': 'inserted', 'record_id': str(uuid4()), 'message': 'Saved',
            'body_characters': len(body), 'media_note_count': 0,
        })
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, 'load_context', return_value=context))
            feed = stack.enter_context(patch.object(module, 'fetch_feed_items', return_value=[entry], side_effect=list_error))
            prepare = stack.enter_context(patch.object(module, 'prepare_candidates', return_value=selection))
            stack.enter_context(patch.object(module, 'read_existing', return_value={}))
            stack.enter_context(patch.object(module, 'select_unseen', return_value=selection))
            fetch = stack.enter_context(patch.object(module, 'fetch_speech' if source == 'ecb' else 'fetch_post',
                                                   return_value=detail, side_effect=fetch_error))
            if source == 'trump':
                stack.enter_context(patch.object(module, 'classify', return_value=('ready_for_import', 'Ready')))
                stack.enter_context(patch.object(module, 'prepare_extraction', return_value={'synthetic': True}))
            clock = stack.enter_context(patch('app.timing.time'))
            # Only the six stage boundaries consume this clock. Other libraries
            # retain their real clocks; sleeps and guard work are not rewritten.
            clock.perf_counter.side_effect = [100.0, 100.2, 101.5, 102.1, 102.2, 102.45]
            before = MagicMock(side_effect=guard_error) if guard_error else None
            result = module.run_import(Settings(_env_file=None), 1, MagicMock(), pause=lambda _: None,
                                       before_request=before, save=saver, feed_items=[entry] if provided else None)
        return result, feed, prepare, fetch, saver

    def test_both_collectors_have_utc_observations_and_monotonic_durations(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, feed, _, fetch, saver = self.run_collector(source)
                self.assertEqual(result['summary']['inserted'], 1)
                timing, item = result['timing'], result['items'][0]['timing']
                self.assertEqual(timing['input_kind'], 'rss_network')
                self.assertTrue(timing['list_succeeded'])
                self.assertEqual(timing['list_duration_ms'], 200.0)
                self.assertEqual(item['list_observed_at'], timing['list_finished_at'])
                self.assertTrue(item['detail_succeeded'])
                self.assertEqual(item['detail_duration_ms'], 600.0)
                self.assertEqual(item['save_duration_ms'], 250.0)
                self.assertEqual(item['observed_to_save_ms'], 2250.0)
                for stamps in (timing, item):
                    for key, stamp in stamps.items():
                        if key.endswith('_at'):
                            self.assertIsNotNone(stamp)
                            self.assertEqual(datetime.fromisoformat(stamp).utcoffset(), timezone.utc.utcoffset(None))
                self.assertGreater(item['list_observed_at'], '2020-01-01T00:00:00Z')
                self.assertNotIn('published_at', timing)
                feed.assert_called_once(); fetch.assert_called_once(); saver.assert_called_once()

    def test_injected_history_items_are_not_reported_as_network_discovery(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, feed, prepare, _, _ = self.run_collector(source, provided=True)
                self.assertEqual(result['timing']['input_kind'], 'provided_items')
                self.assertEqual(result['items'][0]['timing']['input_kind'], 'provided_items')
                feed.assert_not_called(); prepare.assert_called_once()

    def test_failed_detail_retains_elapsed_stage_but_never_confirms_save(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, _, _, _, saver = self.run_collector(source, fetch_error=httpx.ConnectError('DO_NOT_EXPOSE'))
                item = result['items'][0]['timing']
                self.assertEqual(result['status'], 'stopped')
                self.assertFalse(item['detail_succeeded'])
                self.assertIsNotNone(item['detail_finished_at'])
                self.assertEqual(item['detail_duration_ms'], 600.0)
                self.assertIsNone(item['save_started_at'])
                self.assertIsNone(item['save_returned_at'])
                self.assertIsNone(item['observed_to_save_ms'])
                self.assertNotIn('DO_NOT_EXPOSE', str(result))
                saver.assert_not_called()

    def test_failed_writer_does_not_claim_confirmed_return_or_duration(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, _, _, _, _ = self.run_collector(source, save_error=OperationalError('PRIVATE', {}, Exception('PASSWORD')))
                item = result['items'][0]['timing']
                self.assertTrue(item['detail_succeeded'])
                self.assertIsNotNone(item['save_started_at'])
                for key in ('save_returned_at', 'save_duration_ms', 'observed_to_save_ms'):
                    self.assertIsNone(item[key])
                self.assertTrue(result['items'][0]['write_outcome_unknown'])
                self.assertNotIn('PASSWORD', str(result))

    def test_failed_feed_retains_elapsed_observation_but_has_no_candidates(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, _, prepare, fetch, saver = self.run_collector(source, list_error=httpx.ConnectError('PRIVATE'))
                timing = result['timing']
                self.assertFalse(timing['list_succeeded'])
                self.assertIsNotNone(timing['list_finished_at'])
                self.assertEqual(timing['list_duration_ms'], 200.0)
                self.assertEqual(result['items'], [])
                prepare.assert_not_called(); fetch.assert_not_called(); saver.assert_not_called()

    def test_writer_interruption_retains_unknown_commit_and_no_confirmed_timing(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, _, _, _, saver = self.run_collector(source, save_error=KeyboardInterrupt())
                entry = result['items'][0]
                timing = entry['timing']
                self.assertEqual(result['status'], 'interrupted')
                self.assertEqual(result['summary']['attempted'], 1)
                self.assertEqual(result['summary']['interrupted'], 1)
                self.assertFalse(result['saved'])
                self.assertTrue(entry['write_outcome_unknown'])
                self.assertTrue(timing['detail_succeeded'])
                self.assertIsNotNone(timing['save_started_at'])
                for key in ('save_returned_at', 'save_duration_ms', 'observed_to_save_ms'):
                    self.assertIsNone(timing[key])
                saver.assert_called_once()

    def test_detail_interruption_does_not_claim_an_unknown_write(self):
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, _, _, _, saver = self.run_collector(source, fetch_error=KeyboardInterrupt())
                entry = result['items'][0]
                self.assertEqual(result['status'], 'interrupted')
                self.assertFalse(entry['write_outcome_unknown'])
                self.assertFalse(entry['timing']['detail_succeeded'])
                self.assertIsNotNone(entry['timing']['detail_finished_at'])
                self.assertIsNone(entry['timing']['save_started_at'])
                saver.assert_not_called()

    def test_guard_failure_before_request_has_no_fabricated_stage(self):
        from app.collection_guard import CollectionBlocked
        for source in ('ecb', 'trump'):
            with self.subTest(source=source):
                result, feed, _, _, _ = self.run_collector(source, guard_error=CollectionBlocked('PRIVATE'))
                self.assertEqual(result['status'], 'stopped')
                for key in ('list_started_at', 'list_finished_at', 'list_duration_ms', 'list_succeeded'):
                    self.assertIsNone(result['timing'][key])
                feed.assert_not_called()

    def test_helper_never_swallows_interrupt_and_save_is_unconfirmed(self):
        item = item_timing('rss_network', '2026-11-03T00:00:00Z')
        with self.assertRaises(KeyboardInterrupt):
            with measure_stage(item, 'save', success_only=True):
                raise KeyboardInterrupt
        self.assertIsNotNone(item['save_started_at'])
        self.assertIsNone(item['save_returned_at'])
        self.assertIsNone(item['save_duration_ms'])

    def test_duration_rounding_and_non_negative_boundary(self):
        self.assertEqual(elapsed_ms(5.0, 5.0123456), 12.346)
        self.assertEqual(elapsed_ms(6.0, 5.0), 0.0)


if __name__ == '__main__':
    unittest.main()
