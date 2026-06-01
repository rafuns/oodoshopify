"""Tests for pushing a refund Odoo → Shopify (refundCreate)."""
from unittest.mock import patch
from odoo.exceptions import UserError
from .common import ShopifyTestBase


class TestRefundPush(ShopifyTestBase):

    def _setup_order_with_line(self):
        order = self.env['shopify.order'].create({
            'name': '#9001',
            'instance_id': self.instance.id,
            'shopify_order_id': '9001',
        })
        self.env['shopify.order.line'].create({
            'order_id': order.id,
            'shopify_line_id': '5001',
            'shopify_variant_gid': 'gid://shopify/ProductVariant/777',
            'quantity': 2,
            'price': 25.0,
        })
        return order

    def _make_refund(self, order):
        refund = self.env['shopify.refund'].create({
            'name': 'Refund/Test/PUSH',
            'instance_id': self.instance.id,
            'shopify_order_id': order.id,
            'total_refunded': 25.0,
            'note': 'Damaged',
        })
        self.env['shopify.refund.line'].create({
            'refund_id': refund.id,
            'product_name': 'Widget',
            'shopify_variant_gid': 'gid://shopify/ProductVariant/777',
            'quantity': 1,
            'unit_price': 25.0,
        })
        return refund

    def test_push_builds_and_sets_gid(self):
        order = self._setup_order_with_line()
        refund = self._make_refund(order)

        calls = {}

        def fake_graphql(query, variables=None, idempotency_key=None):
            if 'suggestedRefund' in query:
                calls['suggested_vars'] = variables
                return {'order': {'suggestedRefund': {
                    'suggestedTransactions': [{
                        'amountSet': {'shopMoney': {'amount': '25.00'}},
                        'gateway': 'shopify_payments',
                        'kind': 'SUGGESTED_REFUND',
                        'parentTransaction': {'id': 'gid://shopify/OrderTransaction/1'},
                    }],
                }}}
            # refundCreate
            calls['refund_vars'] = variables
            calls['idempotency_key'] = idempotency_key
            return {'refundCreate': {
                'refund': {'id': 'gid://shopify/Refund/55'},
                'userErrors': [],
            }}

        with patch.object(type(self.instance), '_graphql_request', side_effect=fake_graphql):
            refund.action_push_to_shopify()

        # GID + numeric id stored
        self.assertEqual(refund.shopify_refund_gid, 'gid://shopify/Refund/55')
        self.assertEqual(refund.shopify_refund_id, '55')
        # refund line item correctly mapped to the order line GID
        rli = calls['refund_vars']['input']['refundLineItems'][0]
        self.assertEqual(rli['lineItemId'], 'gid://shopify/LineItem/5001')
        self.assertEqual(rli['quantity'], 1)
        # transaction kind normalised to REFUND
        self.assertEqual(calls['refund_vars']['input']['transactions'][0]['kind'], 'REFUND')
        # idempotency key supplied
        self.assertTrue(calls['idempotency_key'])

    def test_push_twice_blocked(self):
        order = self._setup_order_with_line()
        refund = self._make_refund(order)
        refund.shopify_refund_id = '55'
        with self.assertRaises(UserError):
            refund.action_push_to_shopify()

    def test_push_without_lines_blocked(self):
        order = self._setup_order_with_line()
        refund = self.env['shopify.refund'].create({
            'name': 'Refund/Empty',
            'instance_id': self.instance.id,
            'shopify_order_id': order.id,
            'total_refunded': 0.0,
        })
        with self.assertRaises(UserError):
            refund.action_push_to_shopify()


class TestIdempotencyInjection(ShopifyTestBase):

    def test_injects_directive(self):
        q = 'mutation refundCreate($input: RefundInput!) { refundCreate(input: $input) { refund { id } } }'
        out = self.env['shopify.instance']._inject_idempotency(q, 'abc-123')
        self.assertIn('@idempotent(key: "abc-123")', out)
        # directive sits before the operation body brace
        self.assertLess(out.index('@idempotent'), out.index('refundCreate(input'))
