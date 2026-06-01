"""Tests for shopify.customer — import, partner matching, address mapping."""
from .common import ShopifyTestBase, SAMPLE_CUSTOMER_NODE


class TestCustomerUpsert(ShopifyTestBase):

    def test_upsert_creates_customer(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([
            ('instance_id', '=', self.instance.id),
            ('shopify_customer_id', '=', '4242'),
        ])
        self.assertEqual(len(customer), 1)

    def test_upsert_sets_name(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertEqual(customer.name, 'Alice Smith')

    def test_upsert_sets_email(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertEqual(customer.email, 'alice@example.com')

    def test_upsert_sets_orders_count(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertEqual(customer.orders_count, 3)

    def test_upsert_sets_total_spent(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertAlmostEqual(customer.total_spent, 179.97)

    def test_upsert_sets_marketing_consent(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertTrue(customer.accepts_marketing)

    def test_upsert_idempotent(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        count = self.env['shopify.customer'].search_count([
            ('instance_id', '=', self.instance.id),
            ('shopify_customer_id', '=', '4242'),
        ])
        self.assertEqual(count, 1)

    def test_upsert_creates_odoo_partner(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertTrue(customer.odoo_partner_id)

    def test_upsert_partner_has_correct_email(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertEqual(customer.odoo_partner_id.email, 'alice@example.com')

    def test_upsert_partner_has_address(self):
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        partner = customer.odoo_partner_id
        self.assertEqual(partner.street, '123 Main St')
        self.assertEqual(partner.city, 'New York')
        self.assertEqual(partner.zip, '10001')


class TestPartnerMatching(ShopifyTestBase):

    def test_reuses_existing_partner_by_email(self):
        existing = self.env['res.partner'].create({
            'name': 'Alice Smith',
            'email': 'alice@example.com',
        })
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertEqual(customer.odoo_partner_id, existing)

    def test_reuses_existing_partner_by_phone(self):
        existing = self.env['res.partner'].create({
            'name': 'Alice Smith',
            'phone': '+1-555-0100',
        })
        node = dict(SAMPLE_CUSTOMER_NODE, email='different@email.com')
        self.env['shopify.customer']._upsert_customer(self.instance, node)
        customer = self.env['shopify.customer'].search([('shopify_customer_id', '=', '4242')])
        self.assertEqual(customer.odoo_partner_id, existing)

    def test_creates_new_partner_when_no_match(self):
        before_count = self.env['res.partner'].search_count([])
        self.env['shopify.customer']._upsert_customer(self.instance, SAMPLE_CUSTOMER_NODE)
        after_count = self.env['res.partner'].search_count([])
        self.assertGreater(after_count, before_count)


class TestCountryResolution(ShopifyTestBase):

    def test_valid_country_code(self):
        customer = self.env['shopify.customer'].new({'instance_id': self.instance.id})
        country_id = customer._get_country_id('US')
        us = self.env['res.country'].search([('code', '=', 'US')], limit=1)
        self.assertEqual(country_id, us.id)

    def test_lowercase_country_code(self):
        customer = self.env['shopify.customer'].new({'instance_id': self.instance.id})
        country_id = customer._get_country_id('us')
        self.assertTrue(country_id)

    def test_invalid_country_code_returns_false(self):
        customer = self.env['shopify.customer'].new({'instance_id': self.instance.id})
        self.assertFalse(customer._get_country_id('ZZ'))

    def test_empty_country_code_returns_false(self):
        customer = self.env['shopify.customer'].new({'instance_id': self.instance.id})
        self.assertFalse(customer._get_country_id(''))
        self.assertFalse(customer._get_country_id(None))
