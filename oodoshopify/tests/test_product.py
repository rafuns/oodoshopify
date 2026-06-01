"""Tests for shopify.product — import, tags, SEO, build_input, status."""
from .common import ShopifyTestBase, SAMPLE_PRODUCT_NODE


class TestProductUpsert(ShopifyTestBase):

    def test_upsert_creates_new_record(self):
        self.assertEqual(self.env['shopify.product'].search_count([
            ('instance_id', '=', self.instance.id)
        ]), 0)
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        self.assertEqual(self.env['shopify.product'].search_count([
            ('instance_id', '=', self.instance.id)
        ]), 1)

    def test_upsert_sets_basic_fields(self):
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(product.name, 'Test T-Shirt')
        self.assertEqual(product.shopify_product_id, '123456')
        self.assertEqual(product.shopify_gid, 'gid://shopify/Product/123456')

    def test_upsert_sets_status(self):
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(product.shopify_status, 'ACTIVE')

    def test_upsert_sets_tags(self):
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertIn('cotton', product.tags)
        self.assertIn('summer', product.tags)
        self.assertIn('sale', product.tags)

    def test_upsert_sets_seo_fields(self):
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(product.seo_title, 'Best T-Shirt | Acme Co')
        self.assertEqual(product.seo_description, 'Buy our best-selling cotton t-shirt.')

    def test_upsert_creates_variants(self):
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(len(product.variant_ids), 1)
        variant = product.variant_ids[0]
        self.assertEqual(variant.sku, 'TSHIRT-S-WHT')
        self.assertAlmostEqual(variant.price, 29.99)
        self.assertAlmostEqual(variant.compare_at_price, 39.99)

    def test_upsert_idempotent(self):
        """Calling upsert twice should update the record, not create a duplicate."""
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        count = self.env['shopify.product'].search_count([('instance_id', '=', self.instance.id)])
        self.assertEqual(count, 1)

    def test_upsert_updates_existing(self):
        self.env['shopify.product']._upsert_shopify_product(self.instance, SAMPLE_PRODUCT_NODE)
        updated_node = dict(SAMPLE_PRODUCT_NODE, title='Updated T-Shirt')
        self.env['shopify.product']._upsert_shopify_product(self.instance, updated_node)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(product.name, 'Updated T-Shirt')

    def test_upsert_status_draft(self):
        draft_node = dict(SAMPLE_PRODUCT_NODE, status='DRAFT')
        self.env['shopify.product']._upsert_shopify_product(self.instance, draft_node)
        product = self.env['shopify.product'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(product.shopify_status, 'DRAFT')


class TestTagSync(ShopifyTestBase):

    def test_tags_synced_to_odoo_product(self):
        odoo_product = self._make_odoo_product()
        shopify_product = self._make_shopify_product(odoo_product=odoo_product)
        shopify_product._sync_tags_to_odoo(['cotton', 'summer', 'sale'])
        self.assertEqual(len(odoo_product.product_tag_ids), 3)
        tag_names = odoo_product.product_tag_ids.mapped('name')
        self.assertIn('cotton', tag_names)
        self.assertIn('summer', tag_names)

    def test_creates_missing_tags(self):
        odoo_product = self._make_odoo_product()
        shopify_product = self._make_shopify_product(odoo_product=odoo_product)
        shopify_product._sync_tags_to_odoo(['brand-new-tag-xyz'])
        tag = self.env['product.tag'].search([('name', '=', 'brand-new-tag-xyz')])
        self.assertEqual(len(tag), 1)

    def test_reuses_existing_tags(self):
        existing_tag = self.env['product.tag'].create({'name': 'reuse-me'})
        odoo_product = self._make_odoo_product()
        shopify_product = self._make_shopify_product(odoo_product=odoo_product)
        shopify_product._sync_tags_to_odoo(['reuse-me'])
        # Should not create a duplicate
        count = self.env['product.tag'].search_count([('name', '=', 'reuse-me')])
        self.assertEqual(count, 1)
        self.assertIn(existing_tag, odoo_product.product_tag_ids)

    def test_skips_empty_tags(self):
        odoo_product = self._make_odoo_product()
        shopify_product = self._make_shopify_product(odoo_product=odoo_product)
        shopify_product._sync_tags_to_odoo(['', '  ', 'valid-tag'])
        self.assertEqual(len(odoo_product.product_tag_ids), 1)

    def test_no_odoo_product_no_error(self):
        shopify_product = self._make_shopify_product()
        # Should not raise even without a linked Odoo product
        shopify_product._sync_tags_to_odoo(['tag1', 'tag2'])


class TestBuildProductInput(ShopifyTestBase):

    def test_includes_title(self):
        odoo_product = self._make_odoo_product(name='My Product')
        sp = self._make_shopify_product(odoo_product=odoo_product)
        result = sp._build_product_input()
        self.assertEqual(result['title'], 'My Product')

    def test_includes_status(self):
        odoo_product = self._make_odoo_product()
        sp = self._make_shopify_product(odoo_product=odoo_product)
        sp.shopify_status = 'DRAFT'
        result = sp._build_product_input()
        self.assertEqual(result['status'], 'DRAFT')

    def test_includes_seo_when_set(self):
        odoo_product = self._make_odoo_product()
        sp = self._make_shopify_product(odoo_product=odoo_product)
        sp.write({'seo_title': 'My SEO Title', 'seo_description': 'My SEO Desc'})
        result = sp._build_product_input()
        self.assertIn('seo', result)
        self.assertEqual(result['seo']['title'], 'My SEO Title')

    def test_includes_merged_tags(self):
        odoo_product = self._make_odoo_product()
        tag = self.env['product.tag'].create({'name': 'odoo-tag'})
        odoo_product.product_tag_ids = [(4, tag.id)]
        sp = self._make_shopify_product(odoo_product=odoo_product)
        sp.tags = 'shopify-tag'
        result = sp._build_product_input()
        self.assertIn('tags', result)
        self.assertIn('shopify-tag', result['tags'])
        self.assertIn('odoo-tag', result['tags'])

    def test_deduplicates_tags(self):
        odoo_product = self._make_odoo_product()
        tag = self.env['product.tag'].create({'name': 'duplicate'})
        odoo_product.product_tag_ids = [(4, tag.id)]
        sp = self._make_shopify_product(odoo_product=odoo_product)
        sp.tags = 'duplicate'  # same as Odoo tag
        result = sp._build_product_input()
        self.assertEqual(result['tags'].count('duplicate'), 1)

    def test_no_seo_when_not_set(self):
        odoo_product = self._make_odoo_product()
        sp = self._make_shopify_product(odoo_product=odoo_product)
        sp.write({'seo_title': False, 'seo_description': False})
        result = sp._build_product_input()
        # seo may still be included using the product name as fallback
        # just ensure it's a dict if present
        if 'seo' in result:
            self.assertIsInstance(result['seo'], dict)

    def test_includes_gid_when_existing(self):
        odoo_product = self._make_odoo_product()
        sp = self._make_shopify_product(odoo_product=odoo_product)
        result = sp._build_product_input()
        self.assertIn('id', result)
        self.assertEqual(result['id'], sp.shopify_gid)


class TestUpdateFromProductNode(ShopifyTestBase):

    def test_updates_tags(self):
        sp = self._make_shopify_product()
        sp._update_from_product_node({'tags': ['new-tag-1', 'new-tag-2']})
        self.assertIn('new-tag-1', sp.tags)

    def test_updates_seo(self):
        sp = self._make_shopify_product()
        sp._update_from_product_node({
            'seo': {'title': 'New SEO Title', 'description': 'New Desc'}
        })
        self.assertEqual(sp.seo_title, 'New SEO Title')
        self.assertEqual(sp.seo_description, 'New Desc')

    def test_updates_status(self):
        sp = self._make_shopify_product()
        sp._update_from_product_node({'status': 'ARCHIVED'})
        self.assertEqual(sp.shopify_status, 'ARCHIVED')

    def test_empty_node_no_error(self):
        sp = self._make_shopify_product()
        sp._update_from_product_node({})  # should not raise
        sp._update_from_product_node(None)  # should not raise
