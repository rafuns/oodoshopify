"""Tests for shopify.scheduled.push — scheduling and execution."""
from unittest.mock import patch
from datetime import timedelta
from odoo import fields
from odoo.exceptions import UserError
from .common import ShopifyTestBase


class TestScheduledPush(ShopifyTestBase):

    def _make(self, **kw):
        vals = {
            'name': 'Test push',
            'instance_id': self.instance.id,
            'operation': 'push_prices',
            'scheduled_date': fields.Datetime.now() + timedelta(hours=1),
        }
        vals.update(kw)
        return self.env['shopify.scheduled.push'].create(vals)

    def test_schedule_sets_state(self):
        rec = self._make()
        rec.action_schedule()
        self.assertEqual(rec.state, 'scheduled')

    def test_schedule_theme_requires_theme(self):
        rec = self._make(operation='push_theme', theme_id=False)
        with self.assertRaises(UserError):
            rec.action_schedule()

    def test_cancel(self):
        rec = self._make()
        rec.action_schedule()
        rec.action_cancel()
        self.assertEqual(rec.state, 'cancelled')

    def test_execute_calls_push_prices(self):
        rec = self._make(operation='push_prices')
        with patch.object(
            type(self.env['shopify.product']), 'action_push_prices', return_value=True,
        ) as mocked:
            rec._execute()
        self.assertTrue(mocked.called)
        self.assertEqual(rec.state, 'done')
        self.assertTrue(rec.executed_date)

    def test_execute_failure_sets_failed(self):
        rec = self._make(operation='push_prices')
        with patch.object(
            type(self.env['shopify.product']), 'action_push_prices',
            side_effect=Exception('boom'),
        ):
            rec._execute()
        self.assertEqual(rec.state, 'failed')
        self.assertIn('boom', rec.result_message)

    def test_cron_runs_due_only(self):
        past = self._make(scheduled_date=fields.Datetime.now() - timedelta(minutes=5))
        past.action_schedule()
        future = self._make(scheduled_date=fields.Datetime.now() + timedelta(days=1))
        future.action_schedule()
        with patch.object(
            type(self.env['shopify.product']), 'action_push_prices', return_value=True,
        ):
            self.env['shopify.scheduled.push'].cron_run_due()
        self.assertEqual(past.state, 'done')
        self.assertEqual(future.state, 'scheduled')
