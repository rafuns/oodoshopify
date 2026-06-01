"""Tests for shopify.metafield — upsert, type casting, mapping application."""
from .common import ShopifyTestBase, SAMPLE_METAFIELD_EDGES


class TestMetafieldUpsert(ShopifyTestBase):

    def _make_shopify_product_record(self):
        return self._make_shopify_product()

    def test_upsert_creates_metafield_records(self):
        sp = self._make_shopify_product_record()
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        self.assertEqual(len(sp.metafield_ids), 2)

    def test_upsert_sets_namespace_and_key(self):
        sp = self._make_shopify_product_record()
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        material_mf = sp.metafield_ids.filtered(lambda m: m.key == 'material')
        self.assertTrue(material_mf)
        self.assertEqual(material_mf.namespace, 'custom')
        self.assertEqual(material_mf.value, '100% Cotton')

    def test_upsert_sets_value_type(self):
        sp = self._make_shopify_product_record()
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        int_mf = sp.metafield_ids.filtered(lambda m: m.key == 'weight_grams')
        self.assertEqual(int_mf.value_type, 'number_integer')

    def test_upsert_idempotent(self):
        sp = self._make_shopify_product_record()
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        self.assertEqual(len(sp.metafield_ids), 2)

    def test_upsert_updates_value_on_second_call(self):
        sp = self._make_shopify_product_record()
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        updated_edges = [
            {'node': {
                'id': 'gid://shopify/Metafield/9001',
                'namespace': 'custom',
                'key': 'material',
                'value': 'Polyester',
                'type': 'single_line_text_field',
                'updatedAt': '2025-03-10T00:00:00Z',
            }}
        ]
        self.env['shopify.metafield']._upsert_from_nodes(
            updated_edges, self.instance, shopify_product_id=sp.id
        )
        material_mf = sp.metafield_ids.filtered(lambda m: m.key == 'material')
        self.assertEqual(material_mf.value, 'Polyester')

    def test_is_modified_cleared_on_upsert(self):
        sp = self._make_shopify_product_record()
        self.env['shopify.metafield']._upsert_from_nodes(
            SAMPLE_METAFIELD_EDGES, self.instance, shopify_product_id=sp.id
        )
        for mf in sp.metafield_ids:
            self.assertFalse(mf.is_modified)


class TestMetafieldAutoMapping(ShopifyTestBase):

    def test_mapping_applied_to_odoo_product_on_import(self):
        odoo_product = self._make_odoo_product()
        # Add a char field we can write to — use 'description_sale'
        ir_model = self.env['ir.model'].search([('model', '=', 'product.template')], limit=1)
        ir_field = self.env['ir.model.fields'].search([
            ('model_id', '=', ir_model.id),
            ('name', '=', 'description_sale'),
        ], limit=1)

        mapping = self.env['shopify.metafield.mapping'].create({
            'instance_id': self.instance.id,
            'resource_type': 'product',
            'namespace': 'custom',
            'key': 'material',
            'value_type': 'single_line_text_field',
            'direction': 'import',
            'odoo_model_id': ir_model.id,
            'odoo_field_id': ir_field.id,
        })

        sp = self._make_shopify_product(odoo_product=odoo_product)
        self.env['shopify.metafield']._upsert_from_nodes(
            [SAMPLE_METAFIELD_EDGES[0]],  # material = '100% Cotton'
            self.instance,
            shopify_product_id=sp.id,
        )
        self.assertEqual(odoo_product.description_sale, '100% Cotton')

    def test_mapping_sql_unique_constraint(self):
        ir_model = self.env['ir.model'].search([('model', '=', 'product.template')], limit=1)
        self.env['shopify.metafield.mapping'].create({
            'instance_id': self.instance.id,
            'resource_type': 'product',
            'namespace': 'custom',
            'key': 'unique_key',
            'odoo_model_id': ir_model.id,
        })
        from odoo.exceptions import ValidationError
        with self.assertRaises(Exception):
            self.env['shopify.metafield.mapping'].create({
                'instance_id': self.instance.id,
                'resource_type': 'product',
                'namespace': 'custom',
                'key': 'unique_key',
                'odoo_model_id': ir_model.id,
            })
            self.env.flush_all()


class TestMetafieldOwnerGid(ShopifyTestBase):

    def test_product_owner_gid(self):
        sp = self._make_shopify_product()
        mf = self.env['shopify.metafield'].create({
            'shopify_product_id': sp.id,
            'namespace': 'ns',
            'key': 'k',
            'value': 'v',
        })
        self.assertEqual(mf._get_owner_gid(), sp.shopify_gid)

    def test_no_resource_returns_false(self):
        mf = self.env['shopify.metafield'].create({
            'namespace': 'ns',
            'key': 'k',
            'value': 'v',
        })
        self.assertFalse(mf._get_owner_gid())
