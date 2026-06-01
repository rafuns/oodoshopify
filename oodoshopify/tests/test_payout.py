"""Tests for shopify.payout — import, amounts, journal entry creation."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase, SAMPLE_PAYOUT_NODE


class TestPayoutUpsert(ShopifyTestBase):

    def test_upsert_creates_record(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([
            ('instance_id', '=', self.instance.id),
            ('shopify_payout_id', '=', '5050'),
        ])
        self.assertEqual(len(payout), 1)

    def test_upsert_sets_status(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([('shopify_payout_id', '=', '5050')])
        self.assertEqual(payout.status, 'paid')

    def test_upsert_sets_net_amount(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([('shopify_payout_id', '=', '5050')])
        self.assertAlmostEqual(payout.net_amount, 145.50)

    def test_upsert_sets_currency(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([('shopify_payout_id', '=', '5050')])
        self.assertEqual(payout.currency, 'USD')

    def test_upsert_sets_charges_gross(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([('shopify_payout_id', '=', '5050')])
        self.assertAlmostEqual(payout.charges_gross, 179.97)

    def test_upsert_idempotent(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        count = self.env['shopify.payout'].search_count([
            ('instance_id', '=', self.instance.id),
            ('shopify_payout_id', '=', '5050'),
        ])
        self.assertEqual(count, 1)

    def test_total_fees_computed(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([('shopify_payout_id', '=', '5050')])
        # fees: charges_fee=5.40, refunds_fee=0.90, adjustments_fee=0.00 → 6.30
        self.assertAlmostEqual(payout.total_fees, 6.30, places=1)

    def test_initial_reconcile_state_is_pending(self):
        self.env['shopify.payout']._upsert_payout(self.instance, SAMPLE_PAYOUT_NODE)
        payout = self.env['shopify.payout'].search([('shopify_payout_id', '=', '5050')])
        self.assertEqual(payout.reconcile_state, 'pending')


class TestJournalEntry(ShopifyTestBase):

    def _make_payout(self):
        return self.env['shopify.payout'].create({
            'name': 'Payout/5050',
            'instance_id': self.instance.id,
            'shopify_payout_id': '5050',
            'shopify_payout_gid': 'gid://shopify/ShopifyPaymentsPayout/5050',
            'status': 'paid',
            'currency': 'USD',
            'net_amount': 145.50,
            'charges_gross': 179.97,
            'charges_fee': 5.40,
            'refunds_gross': 29.99,
            'refunds_fee': 0.90,
        })

    def _setup_accounting(self):
        company = self.env.company
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', company.id)
        ], limit=1)
        if not journal:
            self.skipTest('No bank journal — accounting chart not configured in this database.')
        clearing_account = self.env['account.account'].create({
            'name': 'Shopify Clearing',
            'code': '999001',
            'account_type': 'liability_current',
            'company_ids': [(6, 0, [company.id])],
        })
        fee_account = self.env['account.account'].create({
            'name': 'Shopify Fees',
            'code': '999002',
            'account_type': 'expense',
            'company_ids': [(6, 0, [company.id])],
        })
        self.instance.write({
            'payout_journal_id': journal.id,
            'shopify_clearing_account_id': clearing_account.id,
            'shopify_fee_account_id': fee_account.id,
        })
        return journal, clearing_account, fee_account

    def test_requires_payout_journal(self):
        payout = self._make_payout()
        self.instance.payout_journal_id = False
        with self.assertRaises(UserError):
            payout.action_create_journal_entry()

    def test_requires_clearing_account(self):
        payout = self._make_payout()
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', self.env.company.id)
        ], limit=1)
        self.instance.payout_journal_id = journal
        self.instance.shopify_clearing_account_id = False
        with self.assertRaises(UserError):
            payout.action_create_journal_entry()

    def test_creates_posted_journal_entry(self):
        payout = self._make_payout()
        self._setup_accounting()
        payout.action_create_journal_entry()
        self.assertTrue(payout.odoo_move_id)
        self.assertEqual(payout.odoo_move_id.state, 'posted')

    def test_reconcile_state_updated_after_entry(self):
        payout = self._make_payout()
        self._setup_accounting()
        payout.action_create_journal_entry()
        self.assertEqual(payout.reconcile_state, 'entry_created')

    def test_duplicate_journal_entry_raises(self):
        payout = self._make_payout()
        self._setup_accounting()
        payout.action_create_journal_entry()
        with self.assertRaises(UserError):
            payout.action_create_journal_entry()

    def test_journal_entry_has_debit_bank_line(self):
        payout = self._make_payout()
        journal, clearing, fee_account = self._setup_accounting()
        payout.action_create_journal_entry()
        bank_account = journal.default_account_id
        bank_lines = payout.odoo_move_id.line_ids.filtered(
            lambda l: l.account_id == bank_account and l.debit > 0
        )
        self.assertTrue(bank_lines)

    def test_journal_entry_has_credit_clearing_line(self):
        payout = self._make_payout()
        journal, clearing, fee_account = self._setup_accounting()
        payout.action_create_journal_entry()
        credit_lines = payout.odoo_move_id.line_ids.filtered(
            lambda l: l.account_id == clearing and l.credit > 0
        )
        self.assertTrue(credit_lines)
