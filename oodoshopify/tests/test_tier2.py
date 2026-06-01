"""Tests for Tier 2 — gift cards, markets, selling plans, store credit."""
from odoo.exceptions import UserError
from .common import (
    ShopifyTestBase, SAMPLE_GIFT_CARD_NODE, SAMPLE_MARKET_NODE,
)


class TestGiftCard(ShopifyTestBase):

    def test_upsert(self):
        self.env['shopify.gift.card']._upsert(self.instance, SAMPLE_GIFT_CARD_NODE)
        rec = self.env['shopify.gift.card'].search([('shopify_gift_card_id', '=', '6001')])
        self.assertEqual(len(rec), 1)
        self.assertAlmostEqual(rec.balance, 45.0)
        self.assertAlmostEqual(rec.initial_value, 50.0)
        self.assertEqual(rec.last_characters, 'AB12')

    def test_create_requires_positive_value(self):
        gc = self.env['shopify.gift.card'].create({
            'name': 'GC', 'instance_id': self.instance.id, 'initial_value': 0,
        })
        with self.assertRaises(UserError):
            gc.action_create_on_shopify()

    def test_create_on_shopify(self):
        gc = self.env['shopify.gift.card'].create({
            'name': 'GC', 'instance_id': self.instance.id, 'initial_value': 25,
        })
        self.mock_gql.return_value = {
            'giftCardCreate': {
                'giftCard': {'id': 'gid://shopify/GiftCard/6100', 'lastCharacters': 'ZZ99',
                             'balance': {'amount': '25.00', 'currencyCode': 'USD'}},
                'giftCardCode': 'ABCD-1234-EFGH-5678', 'userErrors': [],
            },
        }
        gc.action_create_on_shopify()
        self.assertEqual(gc.shopify_gift_card_id, '6100')
        self.assertEqual(gc.generated_code, 'ABCD-1234-EFGH-5678')


class TestMarket(ShopifyTestBase):

    def test_upsert(self):
        self.env['shopify.market']._upsert(self.instance, SAMPLE_MARKET_NODE)
        rec = self.env['shopify.market'].search([('shopify_market_id', '=', '7001')])
        self.assertEqual(rec.name, 'Europe')
        self.assertEqual(rec.base_currency, 'EUR')
        self.assertTrue(rec.enabled)  # status ACTIVE → enabled

    def test_unique_constraint(self):
        self.env['shopify.market'].create({
            'name': 'M', 'instance_id': self.instance.id, 'shopify_market_id': 'DUP',
        })
        with self.assertRaises(Exception):
            self.env['shopify.market'].create({
                'name': 'M2', 'instance_id': self.instance.id, 'shopify_market_id': 'DUP',
            })
            self.env.flush_all()


class TestSellingPlan(ShopifyTestBase):

    def test_create_requires_plans(self):
        group = self.env['shopify.selling.plan.group'].create({
            'name': 'Subs', 'instance_id': self.instance.id,
        })
        with self.assertRaises(UserError):
            group.action_create_on_shopify()

    def test_create_with_plans(self):
        group = self.env['shopify.selling.plan.group'].create({
            'name': 'Monthly Box', 'instance_id': self.instance.id,
            'plan_ids': [(0, 0, {'name': 'Every month', 'interval': 'MONTH',
                                 'interval_count': 1, 'discount_percentage': 10})],
        })
        self.mock_gql.return_value = {
            'sellingPlanGroupCreate': {
                'sellingPlanGroup': {'id': 'gid://shopify/SellingPlanGroup/9100', 'name': 'Monthly Box'},
                'userErrors': [],
            },
        }
        group.action_create_on_shopify()
        self.assertEqual(group.shopify_group_id, '9100')


class TestStoreCredit(ShopifyTestBase):

    def _make_customer(self):
        return self.env['shopify.customer'].create({
            'name': 'Alice', 'instance_id': self.instance.id,
            'shopify_customer_id': '4242',
            'shopify_customer_gid': 'gid://shopify/Customer/4242',
            'email': 'alice@example.com',
        })

    def test_credit_updates_balance(self):
        customer = self._make_customer()
        self.mock_gql.return_value = {
            'storeCreditAccountCredit': {
                'storeCreditAccountTransaction': {
                    'account': {'id': 'gid://shopify/StoreCreditAccount/1',
                                'balance': {'amount': '30.00', 'currencyCode': 'USD'}},
                },
                'userErrors': [],
            },
        }
        customer._adjust_store_credit(30.0, credit=True)
        self.assertAlmostEqual(customer.store_credit_balance, 30.0)

    def test_wizard_rejects_zero(self):
        customer = self._make_customer()
        wizard = self.env['shopify.store.credit.wizard'].create({
            'customer_id': customer.id, 'operation': 'credit', 'amount': 0,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()


class TestOrderTagsAndEditing(ShopifyTestBase):

    def test_push_tags_and_note(self):
        order = self._make_shopify_order()
        order.write({'order_tags': 'vip, wholesale', 'order_note': 'Handle with care'})
        self.mock_gql.return_value = {'orderUpdate': {'order': {'id': order.shopify_order_gid}, 'userErrors': []}}
        order.action_push_tags_and_note()
        # Verify the mock was called with tags as a list
        called_vars = self.mock_gql.call_args.kwargs.get('variables', {})
        self.assertEqual(called_vars['input']['tags'], ['vip', 'wholesale'])

    def test_add_line_item_flow(self):
        order = self._make_shopify_order()
        # Three sequential mutation responses: begin, addVariant, commit
        self.mock_gql.side_effect = [
            {'orderEditBegin': {'calculatedOrder': {'id': 'gid://shopify/CalculatedOrder/1'}, 'userErrors': []}},
            {'orderEditAddVariant': {'calculatedOrder': {'id': 'gid://shopify/CalculatedOrder/1'}, 'userErrors': []}},
            {'orderEditCommit': {'order': {'id': order.shopify_order_gid}, 'userErrors': []}},
        ]
        result = order.action_add_line_item('gid://shopify/ProductVariant/789', quantity=2)
        self.assertTrue(result)
