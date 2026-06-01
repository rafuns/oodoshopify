"""Tests for shopify.instance — GID helpers, currency detection, HMAC, pricelist resolution."""
import hashlib
import hmac
import base64
from odoo.tests.common import TransactionCase
from .common import ShopifyTestBase


class TestGidHelpers(TransactionCase):
    """Static GID helper methods — no DB needed beyond TransactionCase."""

    def _model(self):
        return self.env['shopify.instance']

    def test_gid_to_id_full_gid(self):
        result = self._model()._gid_to_id('gid://shopify/Product/123456')
        self.assertEqual(result, '123456')

    def test_gid_to_id_numeric_passthrough(self):
        result = self._model()._gid_to_id('123456')
        self.assertEqual(result, '123456')

    def test_gid_to_id_empty(self):
        result = self._model()._gid_to_id('')
        self.assertEqual(result, '')

    def test_gid_to_id_variant(self):
        result = self._model()._gid_to_id('gid://shopify/ProductVariant/999')
        self.assertEqual(result, '999')

    def test_build_gid(self):
        result = self._model()._build_gid('Product', '123456')
        self.assertEqual(result, 'gid://shopify/Product/123456')

    def test_build_gid_variant(self):
        result = self._model()._build_gid('ProductVariant', '789')
        self.assertEqual(result, 'gid://shopify/ProductVariant/789')


class TestWebhookSignature(ShopifyTestBase):

    def _make_signature(self, secret, body):
        digest = hmac.new(secret.encode(), body, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode('utf-8')

    def test_valid_signature(self):
        body = b'{"order_id": 123}'
        sig = self._make_signature('my_webhook_secret', body)
        self.assertTrue(self.instance.verify_webhook_signature(body, sig))

    def test_invalid_signature(self):
        body = b'{"order_id": 123}'
        self.assertFalse(self.instance.verify_webhook_signature(body, 'bad_sig'))

    def test_no_webhook_secret(self):
        self.instance.webhook_secret = False
        self.assertFalse(self.instance.verify_webhook_signature(b'data', 'sig'))

    def test_tampered_body(self):
        original = b'{"order_id": 123}'
        tampered = b'{"order_id": 999}'
        sig = self._make_signature('my_webhook_secret', original)
        self.assertFalse(self.instance.verify_webhook_signature(tampered, sig))


class TestOAuthCallbackVerification(ShopifyTestBase):

    def _signed_params(self, secret, params):
        """Build a valid Shopify-style hmac for the given params."""
        message = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
        digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {**params, 'hmac': digest}

    def test_valid_callback_passes(self):
        params = self._signed_params('fake_api_secret', {
            'shop': 'test-store.myshopify.com',
            'code': 'abc123',
            'timestamp': '1700000000',
        })
        self.assertTrue(self.instance._verify_oauth_callback(params))

    def test_tampered_param_fails(self):
        params = self._signed_params('fake_api_secret', {
            'shop': 'test-store.myshopify.com', 'code': 'abc123',
        })
        params['code'] = 'tampered'
        self.assertFalse(self.instance._verify_oauth_callback(params))

    def test_wrong_secret_fails(self):
        params = self._signed_params('wrong_secret', {
            'shop': 'test-store.myshopify.com', 'code': 'abc123',
        })
        self.assertFalse(self.instance._verify_oauth_callback(params))

    def test_shop_mismatch_fails(self):
        params = self._signed_params('fake_api_secret', {
            'shop': 'evil-store.myshopify.com', 'code': 'abc123',
        })
        self.assertFalse(self.instance._verify_oauth_callback(params))

    def test_missing_hmac_fails(self):
        self.assertFalse(self.instance._verify_oauth_callback({
            'shop': 'test-store.myshopify.com', 'code': 'abc123',
        }))


class TestConnectedMenuGroup(ShopifyTestBase):

    def _connected_group(self):
        return self.env.ref('oodoshopify.shopify_group_connected')

    def test_group_populated_when_connected(self):
        # self.instance is created with state='connected' in common setUp
        self.instance._sync_connected_menu_group()
        self.assertTrue(self._connected_group().user_ids,
                        'Connected group should have members when a store is connected')

    def test_group_cleared_when_none_connected(self):
        self.instance.write({'state': 'draft'})
        self.assertFalse(
            self.env['shopify.instance'].search([('state', '=', 'connected')]),
            'precondition: no connected instances')
        self.assertFalse(self._connected_group().user_ids,
                         'Connected group should be empty when nothing is connected')


class TestPricelistResolution(ShopifyTestBase):

    def test_no_map_returns_default_pricelist(self):
        pl = self.env['product.pricelist'].create({
            'name': 'USD Pricelist',
            'currency_id': self.env.ref('base.USD').id,
        })
        self.instance.pricelist_id = pl
        result = self.instance._get_pricelist_for_currency('USD')
        self.assertEqual(result, pl)

    def test_explicit_map_takes_priority(self):
        eur = self.env.ref('base.EUR')
        eur.active = True
        pl_usd = self.env['product.pricelist'].create({
            'name': 'USD Pricelist',
            'currency_id': self.env.ref('base.USD').id,
        })
        pl_eur = self.env['product.pricelist'].create({
            'name': 'EUR Pricelist',
            'currency_id': eur.id,
        })
        self.instance.pricelist_id = pl_usd
        self.env['shopify.pricelist.map'].create({
            'instance_id': self.instance.id,
            'currency_id': eur.id,
            'pricelist_id': pl_eur.id,
        })
        result = self.instance._get_pricelist_for_currency('EUR')
        self.assertEqual(result, pl_eur)

    def test_fallback_to_default_when_no_match(self):
        pl = self.env['product.pricelist'].create({
            'name': 'USD Pricelist',
            'currency_id': self.env.ref('base.USD').id,
        })
        self.instance.pricelist_id = pl
        result = self.instance._get_pricelist_for_currency('JPY')
        self.assertEqual(result, pl)

    def test_no_pricelist_returns_false(self):
        self.instance.pricelist_id = False
        result = self.instance._get_pricelist_for_currency('USD')
        self.assertFalse(result)

    def test_currency_code_case_insensitive(self):
        pl = self.env['product.pricelist'].create({
            'name': 'USD Pricelist',
            'currency_id': self.env.ref('base.USD').id,
        })
        self.instance.pricelist_id = pl
        self.assertEqual(
            self.instance._get_pricelist_for_currency('usd'),
            self.instance._get_pricelist_for_currency('USD'),
        )


class TestWebhookConfigInit(ShopifyTestBase):

    def test_init_creates_all_topics(self):
        from odoo.addons.oodoshopify.models.shopify_webhook_config import WEBHOOK_TOPIC_PATHS
        self.assertEqual(len(self.instance.webhook_config_ids), 0)
        self.instance._init_webhook_configs()
        self.assertEqual(len(self.instance.webhook_config_ids), len(WEBHOOK_TOPIC_PATHS))

    def test_init_all_enabled_by_default(self):
        self.instance._init_webhook_configs()
        disabled = self.instance.webhook_config_ids.filtered(lambda c: not c.is_enabled)
        self.assertEqual(len(disabled), 0)

    def test_init_idempotent(self):
        from odoo.addons.oodoshopify.models.shopify_webhook_config import WEBHOOK_TOPIC_PATHS
        self.instance._init_webhook_configs()
        self.instance._init_webhook_configs()  # second call should not duplicate
        self.assertEqual(len(self.instance.webhook_config_ids), len(WEBHOOK_TOPIC_PATHS))

    def test_topic_labels_set(self):
        self.instance._init_webhook_configs()
        for config in self.instance.webhook_config_ids:
            self.assertTrue(config.topic_label, f'topic_label missing for {config.topic}')
