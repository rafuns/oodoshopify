"""Tests for shopify.collection."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase

SAMPLE_CUSTOM_COLLECTION = {
    'id': 'gid://shopify/Collection/2001',
    'title': 'Featured', 'handle': 'featured', 'descriptionHtml': '<p>Top picks</p>',
    'updatedAt': '2025-03-01T00:00:00Z',
    'productsCount': {'count': 8},
    'image': {'url': 'https://cdn.shopify.com/feat.png'},
    'ruleSet': None,
}

SAMPLE_SMART_COLLECTION = {
    'id': 'gid://shopify/Collection/2002',
    'title': 'Under $50', 'handle': 'under-50', 'descriptionHtml': '',
    'updatedAt': '2025-03-01T00:00:00Z',
    'productsCount': {'count': 20},
    'image': None,
    'ruleSet': {'appliedDisjunctively': False,
                'rules': [{'column': 'VARIANT_PRICE', 'relation': 'LESS_THAN', 'condition': '50'}]},
}


class TestCollectionUpsert(ShopifyTestBase):

    def test_custom_collection(self):
        self.env['shopify.collection']._upsert_collection(self.instance, SAMPLE_CUSTOM_COLLECTION)
        rec = self.env['shopify.collection'].search([('shopify_collection_id', '=', '2001')])
        self.assertEqual(rec.collection_type, 'custom')
        self.assertEqual(rec.products_count, 8)

    def test_smart_collection_with_rules(self):
        self.env['shopify.collection']._upsert_collection(self.instance, SAMPLE_SMART_COLLECTION)
        rec = self.env['shopify.collection'].search([('shopify_collection_id', '=', '2002')])
        self.assertEqual(rec.collection_type, 'smart')
        self.assertEqual(len(rec.rule_ids), 1)
        self.assertEqual(rec.rule_ids.column, 'VARIANT_PRICE')

    def test_idempotent(self):
        self.env['shopify.collection']._upsert_collection(self.instance, SAMPLE_CUSTOM_COLLECTION)
        self.env['shopify.collection']._upsert_collection(self.instance, SAMPLE_CUSTOM_COLLECTION)
        self.assertEqual(
            self.env['shopify.collection'].search_count([('shopify_collection_id', '=', '2001')]), 1
        )


class TestCollectionActions(ShopifyTestBase):

    def test_smart_cannot_sync_products(self):
        col = self.env['shopify.collection'].create({
            'name': 'Smart', 'instance_id': self.instance.id,
            'collection_type': 'smart',
            'shopify_collection_gid': 'gid://shopify/Collection/2002',
        })
        with self.assertRaises(UserError):
            col.action_sync_products_to_shopify()

    def test_push_custom_collection(self):
        col = self.env['shopify.collection'].create({
            'name': 'New Collection', 'instance_id': self.instance.id, 'collection_type': 'custom',
        })
        self.mock_gql.return_value = {
            'collectionCreate': {'collection': {'id': 'gid://shopify/Collection/2050',
                                                'title': 'New Collection', 'handle': 'new-collection'},
                                 'userErrors': []},
        }
        col.action_push_to_shopify()
        self.assertEqual(col.shopify_collection_id, '2050')
        self.assertEqual(col.sync_status, 'synced')
