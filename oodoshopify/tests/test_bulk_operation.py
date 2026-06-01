"""Tests for shopify.bulk.operation."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase


class TestBulkOperation(ShopifyTestBase):

    def test_start_sets_running(self):
        op = self.env['shopify.bulk.operation'].create({
            'name': 'Export', 'instance_id': self.instance.id, 'operation_type': 'products',
        })
        self.mock_gql.return_value = {
            'bulkOperationRunQuery': {
                'bulkOperation': {'id': 'gid://shopify/BulkOperation/1', 'status': 'CREATED'},
                'userErrors': [],
            },
        }
        op.action_start()
        self.assertEqual(op.shopify_bulk_gid, 'gid://shopify/BulkOperation/1')
        self.assertEqual(op.status, 'running')

    def test_only_one_running_per_instance(self):
        self.env['shopify.bulk.operation'].create({
            'name': 'Running', 'instance_id': self.instance.id,
            'operation_type': 'products', 'status': 'running',
        })
        op2 = self.env['shopify.bulk.operation'].create({
            'name': 'Second', 'instance_id': self.instance.id, 'operation_type': 'orders',
        })
        with self.assertRaises(UserError):
            op2.action_start()

    def test_custom_query_required(self):
        op = self.env['shopify.bulk.operation'].create({
            'name': 'Custom', 'instance_id': self.instance.id, 'operation_type': 'custom',
        })
        with self.assertRaises(UserError):
            op.action_start()

    def test_refresh_status_updates_fields(self):
        op = self.env['shopify.bulk.operation'].create({
            'name': 'Poll', 'instance_id': self.instance.id, 'operation_type': 'products',
            'shopify_bulk_gid': 'gid://shopify/BulkOperation/2', 'status': 'running',
        })
        self.mock_gql.return_value = {
            'currentBulkOperation': {
                'id': 'gid://shopify/BulkOperation/2', 'status': 'COMPLETED',
                'objectCount': 500, 'fileSize': 10240,
                'url': 'https://shopify.example/result.jsonl', 'errorCode': None,
            },
        }
        op.action_refresh_status()
        self.assertEqual(op.status, 'completed')
        self.assertEqual(op.object_count, 500)
        self.assertTrue(op.result_url)

    def test_process_requires_completed(self):
        op = self.env['shopify.bulk.operation'].create({
            'name': 'NotDone', 'instance_id': self.instance.id,
            'operation_type': 'products', 'status': 'running',
        })
        with self.assertRaises(UserError):
            op.action_process_result()
