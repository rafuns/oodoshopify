"""Tests for the background historical-import queue path."""
import json
from unittest.mock import patch
from .common import ShopifyTestBase


class TestHistoricalImportQueue(ShopifyTestBase):

    def test_enqueue_creates_pending_job(self):
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'orders', {'days_back': 365})
        self.assertEqual(job.operation, 'historical_import')
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.instance_id, self.instance)
        payload = json.loads(job.payload)
        self.assertEqual(payload['kind'], 'orders')
        self.assertEqual(payload['days_back'], 365)

    def test_run_dispatches_orders(self):
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'orders', {'status': 'any', 'days_back': 30})
        with patch.object(
            type(self.env['shopify.order']), 'import_orders_from_shopify',
            return_value=0,
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)
        # date filters serialize/deserialize without error
        self.assertEqual(mocked.call_args.kwargs.get('status'), 'any')

    def test_run_dispatches_products(self):
        job = self.env['shopify.queue'].enqueue_historical_import(self.instance, 'products')
        with patch.object(
            type(self.env['shopify.product']), 'import_products_from_shopify',
            return_value=0,
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)

    def test_run_dispatches_customers(self):
        job = self.env['shopify.queue'].enqueue_historical_import(self.instance, 'customers')
        with patch.object(
            type(self.env['shopify.customer']), 'import_customers_from_shopify',
            return_value=0,
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)

    def test_chunk_chains_next_page(self):
        """A chunk with has_next=True should enqueue a follow-up job."""
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'products', {'chunked': True})
        before = self.env['shopify.queue'].search_count([('operation', '=', 'historical_import')])
        with patch.object(
            type(self.env['shopify.product']), 'import_products_page',
            return_value=(50, 'CURSOR2', True),
        ):
            job._run_historical_import()
        after = self.env['shopify.queue'].search_count([('operation', '=', 'historical_import')])
        self.assertEqual(after, before + 1, 'a follow-up chunk job should be queued')
        nxt = self.env['shopify.queue'].search(
            [('operation', '=', 'historical_import')], order='id desc', limit=1)
        self.assertEqual(json.loads(nxt.payload).get('cursor'), 'CURSOR2')

    def test_chunk_stops_on_last_page(self):
        """A chunk with has_next=False should NOT enqueue another job."""
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'products', {'chunked': True})
        before = self.env['shopify.queue'].search_count([('operation', '=', 'historical_import')])
        with patch.object(
            type(self.env['shopify.product']), 'import_products_page',
            return_value=(12, None, False),
        ):
            job._run_historical_import()
        after = self.env['shopify.queue'].search_count([('operation', '=', 'historical_import')])
        self.assertEqual(after, before, 'no follow-up job on the last page')

    def test_chunk_orders_passes_create_sale_orders(self):
        """create_sale_orders flag should reach import_orders_page."""
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'orders',
            {'chunked': True, 'query_filter': '', 'create_sale_orders': True})
        with patch.object(
            type(self.env['shopify.order']), 'import_orders_page',
            return_value=(0, None, False),
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)
        self.assertTrue(mocked.call_args.kwargs.get('create_sale_orders'))

    def test_chunk_dispatches_collections(self):
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'collections', {'chunked': True})
        with patch.object(
            type(self.env['shopify.collection']), 'import_collections_page',
            return_value=(0, None, False),
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)

    def test_chunk_dispatches_payouts(self):
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'payouts', {'chunked': True})
        with patch.object(
            type(self.env['shopify.payout']), 'import_payouts_page',
            return_value=(0, None, False),
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)

    def test_chunk_dispatches_abandoned(self):
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'abandoned', {'chunked': True, 'query_filter': ''})
        with patch.object(
            type(self.env['shopify.abandoned.checkout']), 'import_checkouts_page',
            return_value=(0, None, False),
        ) as mocked:
            job._run_historical_import()
        self.assertTrue(mocked.called)

    def test_unknown_kind_raises(self):
        job = self.env['shopify.queue'].create({
            'name': 'bad', 'instance_id': self.instance.id,
            'operation': 'historical_import', 'payload': json.dumps({'kind': 'nope'}),
            'state': 'pending',
        })
        with self.assertRaises(ValueError):
            job._run_historical_import()


class TestImportJobKindsComplete(ShopifyTestBase):
    """Every kind the queue can enqueue must be a valid import.job kind."""

    def test_all_kinds_valid(self):
        job_kinds = dict(self.env['shopify.import.job']._fields['kind'].selection)
        for kind in ('orders', 'products', 'customers', 'collections',
                     'payouts', 'abandoned', 'discounts'):
            self.assertIn(kind, job_kinds, "import.job.kind missing '%s'" % kind)

    def test_enqueue_discounts_creates_job(self):
        job = self.env['shopify.queue'].enqueue_historical_import(
            self.instance, 'discounts', {'chunked': True})
        self.assertEqual(job.operation, 'historical_import')
        import_job = self.env['shopify.import.job'].search(
            [('kind', '=', 'discounts')], limit=1)
        self.assertTrue(import_job)
