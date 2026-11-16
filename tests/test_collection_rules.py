import io
import itertools
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.check_collection_rules import main
from app.collection_rules import SUPPORTED, check_rules, evaluate_rule
from app.core.config import Settings
from app.core.database import create_read_engine, database_url
from app.main import create_app
from app.repositories.collection_rules import read_contexts, select_context


def context(source='ecb', **changes):
    sid, pid, url, provenance = SUPPORTED[source]
    return dict(source_id=sid, person_id=pid, feed_url=url, provenance_type=provenance,
                fetch_method='rss', source_name='Synthetic source', person_name='Synthetic person',
                link_source_id=sid, link_person_id=pid,
                **{**dict(poll_interval_seconds=300, source_enabled=True, monitoring_enabled=True, link_enabled=True), **changes})


class CollectionRuleTests(unittest.TestCase):
    def test_three_switch_truth_table_for_each_supported_adapter(self):
        for source in SUPPORTED:
            for flags in itertools.product((False, True), repeat=3):
                changes = dict(zip(('source_enabled', 'monitoring_enabled', 'link_enabled'), flags))
                result = evaluate_rule(source, context(source, **changes))
                self.assertEqual(result.eligible, all(flags))
                self.assertEqual(result.status, 'eligible' if all(flags) else 'disabled')
                if not all(flags):
                    self.assertEqual(len(result.reasons), flags.count(False))

    def test_missing_identity_link_and_unknown_switches_fail_closed(self):
        for field, value in (('source_id', None), ('person_id', uuid4()), ('link_source_id', None),
                             ('link_person_id', uuid4()), ('source_enabled', 1), ('link_enabled', 'true'),
                             ('monitoring_enabled', None)):
            row = context()
            row[field] = value
            with self.subTest(field=field):
                result = evaluate_rule('ecb', row)
                self.assertFalse(result.eligible)
                self.assertEqual(result.status, 'invalid_configuration')

    def test_endpoint_category_method_and_interval_must_match(self):
        for source in SUPPORTED:
            for field, value in (('feed_url', 'https://example.invalid/feed'),
                                 ('feed_url', SUPPORTED[source][2] + '?changed'),
                                 ('provenance_type', 'other'), ('fetch_method', 'browser'),
                                 ('poll_interval_seconds', 59), ('poll_interval_seconds', True),
                                 ('poll_interval_seconds', '300')):
                row = context(source)
                row[field] = value
                self.assertFalse(evaluate_rule(source, row).eligible)
            self.assertTrue(evaluate_rule(source, context(source, poll_interval_seconds=60)).eligible)

    def test_disabled_flags_do_not_hide_invalid_config_and_unknown_adapter_rejected(self):
        row = context(source_enabled=False)
        row['feed_url'] = 'https://example.invalid/'
        result = evaluate_rule('ecb', row)
        self.assertEqual(result.status, 'invalid_configuration')
        self.assertIn('source_enabled_off', [reason.code for reason in result.reasons])
        with self.assertRaises(ValueError):
            evaluate_rule('other', context())

    def test_check_report_counts_and_source_selection_without_running_collector(self):
        with patch('app.repositories.collection_rules.read_contexts', return_value={'ecb': context()}) as read:
            result = check_rules(Settings(_env_file=None), 'ecb')
        self.assertEqual(read.call_args.args[1], ['ecb'])
        self.assertEqual(result.eligible_count, 1)
        self.assertFalse(result.automatic_started)
        self.assertFalse(result.changes_saved)
        self.assertFalse(result.network_requested)
        self.assertEqual((result.default_batch_limit, result.max_batch_limit), (3, 5))

    def test_api_source_filter_disabled_and_database_errors(self):
        with patch('app.repositories.collection_rules.read_contexts', return_value={'trump': context('trump', source_enabled=False)}):
            with TestClient(create_app(Settings(_env_file=None))) as client:
                response = client.get('/api/v1/collection-rules?source=trump')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['items'][0]['status'], 'disabled')
                self.assertEqual(client.get('/api/v1/collection-rules?source=unknown').status_code, 422)
                self.assertEqual(client.post('/api/v1/collection-rules').status_code, 405)
        with patch('app.api.collection_rules.check_rules', side_effect=OperationalError('PRIVATE SQL', {}, Exception('SECRET'))):
            with TestClient(create_app(Settings(_env_file=None))) as client:
                response = client.get('/api/v1/collection-rules')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('SECRET', response.text)

    def test_cli_validates_arguments_before_io_and_redacts_failure(self):
        with patch('app.check_collection_rules.Settings') as settings, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--source', 'unknown'])
        settings.assert_not_called()
        output = io.StringIO()
        with patch('app.check_collection_rules.check_rules', side_effect=ValueError('SECRET')), redirect_stdout(output):
            self.assertEqual(main([]), 1)
        self.assertNotIn('SECRET', output.getvalue())

    def test_cli_disabled_is_normal_invalid_config_needs_review(self):
        for row, expected in ((context(source_enabled=False), 0), (context(poll_interval_seconds=1), 2)):
            output = io.StringIO()
            with patch('app.repositories.collection_rules.read_contexts', return_value={'ecb': row}), redirect_stdout(output):
                self.assertEqual(main(['--source', 'ecb']), expected)
            self.assertFalse(json.loads(output.getvalue())['automatic_started'])


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class CollectionRuleDatabaseTests(unittest.TestCase):
    def test_real_rule_check_is_readonly_and_does_not_alter_configuration_content_or_history(self):
        settings = Settings()
        engine = create_read_engine(settings)
        def snapshot():
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SHOW transaction_read_only')).scalar_one(), 'on')
                return {name: [dict(row) for row in connection.execute(text('SELECT * FROM ' + name)).mappings()]
                        for name in ('people', 'sources', 'person_sources', 'public_records', 'archive_post_details', 'collection_runs', 'collection_run_items')}
        try:
            before = snapshot()
            result = check_rules(settings)
            self.assertEqual(len(result.items), 2)
            self.assertEqual(result.eligible_count, 0)
            self.assertTrue(all(item.status == 'disabled' for item in result.items))
            self.assertEqual(snapshot(), before)
        finally:
            engine.dispose()

    def test_all_switches_and_each_independent_veto_using_rolled_back_updates(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    for source, (sid, pid, _, _) in SUPPORTED.items():
                        connection.execute(text('UPDATE sources SET enabled=true WHERE source_id=:id'), {'id': sid})
                        connection.execute(text('UPDATE people SET monitoring_enabled=true WHERE person_id=:id'), {'id': pid})
                        connection.execute(text('UPDATE person_sources SET enabled=true WHERE source_id=:sid AND person_id=:pid'), {'sid': sid, 'pid': pid})
                        self.assertTrue(evaluate_rule(source, select_context(connection, source)).eligible)
                        for sql in ('UPDATE sources SET enabled=false WHERE source_id=:sid',
                                    'UPDATE people SET monitoring_enabled=false WHERE person_id=:pid',
                                    'UPDATE person_sources SET enabled=false WHERE source_id=:sid AND person_id=:pid'):
                            savepoint = connection.begin_nested()
                            connection.execute(text(sql), {'sid': sid, 'pid': pid})
                            self.assertFalse(evaluate_rule(source, select_context(connection, source)).eligible)
                            savepoint.rollback()
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()

    def test_missing_link_changed_url_and_short_interval_are_invalid_without_commits(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        sid, pid, _, _ = SUPPORTED['ecb']
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    for sql in ('DELETE FROM person_sources WHERE source_id=:sid AND person_id=:pid',
                                "UPDATE sources SET feed_url='https://example.invalid/' WHERE source_id=:sid",
                                'UPDATE sources SET poll_interval_seconds=1 WHERE source_id=:sid'):
                        savepoint = connection.begin_nested()
                        connection.execute(text(sql), {'sid': sid, 'pid': pid})
                        result = evaluate_rule('ecb', select_context(connection, 'ecb'))
                        self.assertFalse(result.eligible)
                        self.assertEqual(result.status, 'invalid_configuration')
                        savepoint.rollback()
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
