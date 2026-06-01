import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

QUERY_PAYOUTS = '''
    query GetPayouts($first: Int!, $after: String) {
        shopifyPaymentsAccount {
            payouts(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        issuedAt
                        status
                        net { amount currencyCode }
                        summary {
                            adjustmentsGross  { amount currencyCode }
                            adjustmentsFee    { amount currencyCode }
                            chargesGross      { amount currencyCode }
                            chargesFee        { amount currencyCode }
                            refundsFeeGross   { amount currencyCode }
                            refundsFee        { amount currencyCode }
                        }
                    }
                }
            }
        }
    }
'''

QUERY_PAYOUT_TRANSACTIONS = '''
    query GetPayoutTransactions($payoutId: ID!, $first: Int!, $after: String) {
        shopifyPaymentsAccount {
            transactions(payoutId: $payoutId, first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        type
                        test
                        sourceOrderName
                        fee   { amount currencyCode }
                        net   { amount currencyCode }
                        gross { amount currencyCode }
                    }
                }
            }
        }
    }
'''

# Shopify payout status → selection value
_STATUS_MAP = {
    'SCHEDULED':  'scheduled',
    'IN_TRANSIT': 'in_transit',
    'PAID':       'paid',
    'FAILED':     'failed',
    'CANCELLED':  'cancelled',
}

# Shopify transaction type → human label
_TX_TYPE_LABELS = {
    'PAYOUT':                   'Payout',
    'REFUND':                   'Refund',
    'DISPUTE':                  'Dispute',
    'RESERVE':                  'Reserve',
    'ADJUSTMENT':               'Adjustment',
    'CREDIT':                   'Credit',
    'DEBIT':                    'Debit',
    'PAYOUT_FAILURE':           'Payout Failure',
    'PAYOUT_CANCELLATION':      'Payout Cancellation',
    'RESERVED_FUNDS_RELEASED':  'Reserved Funds Released',
    'TRANSFER':                 'Transfer',
}


class ShopifyPayout(models.Model):
    _name = 'shopify.payout'
    _description = 'Shopify Payout'
    _inherit = ['mail.thread']
    _order = 'issued_at desc'

    name = fields.Char(string='Payout Reference', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )

    # Shopify identity
    shopify_payout_id  = fields.Char(string='Payout ID',  readonly=True)
    shopify_payout_gid = fields.Char(string='Payout GID', readonly=True)

    # Core fields
    issued_at = fields.Datetime(string='Issued At', readonly=True)
    status = fields.Selection([
        ('scheduled',  'Scheduled'),
        ('in_transit', 'In Transit'),
        ('paid',       'Paid'),
        ('failed',     'Failed'),
        ('cancelled',  'Cancelled'),
    ], string='Status', tracking=True)

    currency = fields.Char(string='Currency')
    net_amount = fields.Float(string='Net Payout', digits=(16, 2))

    # Summary breakdown
    charges_gross  = fields.Float(string='Sales (Gross)',       digits=(16, 2))
    charges_fee    = fields.Float(string='Sales Fee',           digits=(16, 2))
    refunds_gross  = fields.Float(string='Refunds (Gross)',     digits=(16, 2))
    refunds_fee    = fields.Float(string='Refunds Fee',         digits=(16, 2))
    adjustments_gross = fields.Float(string='Adjustments',     digits=(16, 2))
    adjustments_fee   = fields.Float(string='Adjustments Fee', digits=(16, 2))

    total_fees = fields.Float(
        string='Total Fees', compute='_compute_total_fees', store=True, digits=(16, 2),
    )

    # Odoo accounting
    odoo_move_id = fields.Many2one(
        'account.move', string='Journal Entry', readonly=True,
    )
    reconcile_state = fields.Selection([
        ('pending',     'Pending'),
        ('entry_created', 'Entry Created'),
        ('reconciled',  'Reconciled'),
    ], string='Reconcile State', default='pending', tracking=True)

    transaction_ids = fields.One2many(
        'shopify.payout.transaction', 'payout_id', string='Transactions',
    )
    transaction_count = fields.Integer(compute='_compute_tx_count')

    @api.depends('charges_fee', 'refunds_fee', 'adjustments_fee')
    def _compute_total_fees(self):
        for rec in self:
            rec.total_fees = (
                abs(rec.charges_fee) +
                abs(rec.refunds_fee) +
                abs(rec.adjustments_fee)
            )

    @api.depends('transaction_ids')
    def _compute_tx_count(self):
        for rec in self:
            rec.transaction_count = len(rec.transaction_ids)

    # ------------------------------------------------------------------
    # Import payouts
    # ------------------------------------------------------------------

    @api.model
    def import_payouts_from_shopify(self, instance, limit=50):
        """Fetch recent payouts from Shopify via shopifyPaymentsAccount."""
        try:
            cursor = None
            imported = 0
            while True:
                count, cursor, has_next = self.import_payouts_page(instance, cursor)
                imported += count
                if not has_next or imported >= limit:
                    break
            _logger.info('Imported %d payouts from %s', imported, instance.name)
            return imported
        except UserError as e:
            _logger.warning('Payout import failed for %s: %s', instance.name, e)
            return 0

    def import_payouts_page(self, instance, cursor=None):
        """Import one page of payouts → (count, next_cursor, has_next).
        Used by the chunked background importer so each job stays small."""
        variables = {'first': 50, 'after': cursor}
        data = instance._graphql_request(QUERY_PAYOUTS, variables=variables)
        account = data.get('shopifyPaymentsAccount')
        if not account:
            _logger.warning(
                '%s: shopifyPaymentsAccount returned null — '
                'store may not use Shopify Payments.', instance.name)
            return 0, None, False
        connection = account.get('payouts', {})
        edges = connection.get('edges', [])
        for edge in edges:
            self._upsert_payout(instance, edge['node'])
        page_info = connection.get('pageInfo', {})
        return len(edges), page_info.get('endCursor'), page_info.get('hasNextPage', False)

    def _upsert_payout(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_payout_id', '=', numeric_id),
        ], limit=1)

        net    = node.get('net', {})
        smry   = node.get('summary', {})
        issued = (node.get('issuedAt') or '').replace('T', ' ').replace('Z', '')

        def _amt(d):
            return float((d or {}).get('amount', 0) or 0)

        vals = {
            'name':              f'Payout/{numeric_id}',
            'instance_id':       instance.id,
            'shopify_payout_id': numeric_id,
            'shopify_payout_gid': gid,
            'issued_at':         issued or False,
            'status':            _STATUS_MAP.get(node.get('status', ''), 'scheduled'),
            'currency':          net.get('currencyCode', ''),
            'net_amount':        _amt(net),
            'charges_gross':     _amt(smry.get('chargesGross')),
            'charges_fee':       _amt(smry.get('chargesFee')),
            'refunds_gross':     _amt(smry.get('refundsFeeGross')),
            'refunds_fee':       _amt(smry.get('refundsFee')),
            'adjustments_gross': _amt(smry.get('adjustmentsGross')),
            'adjustments_fee':   _amt(smry.get('adjustmentsFee')),
        }
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)

    # ------------------------------------------------------------------
    # Fetch transactions for this payout
    # ------------------------------------------------------------------

    def action_fetch_transactions(self):
        """Load all transactions for this payout from Shopify."""
        self.ensure_one()
        if not self.shopify_payout_gid:
            raise UserError(_('No Shopify GID — import payouts first.'))
        cursor = None
        fetched = 0
        while True:
            variables = {
                'payoutId': self.shopify_payout_gid,
                'first': 100,
                'after': cursor,
            }
            data = self.instance_id._graphql_request(
                QUERY_PAYOUT_TRANSACTIONS, variables=variables
            )
            account = data.get('shopifyPaymentsAccount') or {}
            connection = account.get('transactions', {})
            edges = connection.get('edges', [])

            for edge in edges:
                self._upsert_transaction(edge['node'])
                fetched += 1

            page_info = connection.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Transactions Loaded'),
                'message': _('Loaded %d transactions for %s.') % (fetched, self.name),
                'type': 'success',
            },
        }

    def _upsert_transaction(self, node):
        tx_id = self.instance_id._gid_to_id(node['id'])
        existing = self.env['shopify.payout.transaction'].search([
            ('payout_id', '=', self.id),
            ('shopify_tx_id', '=', tx_id),
        ], limit=1)

        def _amt(d):
            return float((d or {}).get('amount', 0) or 0)

        vals = {
            'payout_id':        self.id,
            'shopify_tx_id':    tx_id,
            'shopify_tx_gid':   node['id'],
            'tx_type':          node.get('type', 'TRANSFER'),
            'tx_type_label':    _TX_TYPE_LABELS.get(node.get('type', ''), node.get('type', '')),
            'is_test':          node.get('test', False),
            'source_order_name': node.get('sourceOrderName', '') or '',
            'gross_amount':     _amt(node.get('gross')),
            'fee_amount':       _amt(node.get('fee')),
            'net_amount':       _amt(node.get('net')),
        }
        if existing:
            existing.write(vals)
        else:
            self.env['shopify.payout.transaction'].create(vals)

    # ------------------------------------------------------------------
    # Odoo journal entry creation
    # ------------------------------------------------------------------

    def action_create_journal_entry(self):
        """
        Create an Odoo account.move for this payout.

        Journal entry structure:
          DR  Bank / Payout account        net_amount      (money received)
          DR  Shopify Fees Expense         total_fees      (fees charged by Shopify)
          CR  Shopify Clearing account     (net + fees)    (clears the receivable)
        """
        self.ensure_one()
        if self.odoo_move_id:
            raise UserError(_('Journal entry already exists: %s') % self.odoo_move_id.name)

        instance = self.instance_id
        if not instance.payout_journal_id:
            raise UserError(_(
                'No Payout Journal configured on instance "%s". '
                'Go to the instance → Accounting section.'
            ) % instance.name)
        if not instance.shopify_clearing_account_id:
            raise UserError(_(
                'No Shopify Clearing Account configured on instance "%s".'
            ) % instance.name)

        currency = self.env['res.currency'].search(
            [('name', '=', (self.currency or '').upper())], limit=1
        )
        company = instance.company_id or self.env.company

        line_vals = []

        # DR Bank
        bank_account = instance.payout_journal_id.default_account_id
        if not bank_account:
            raise UserError(_('Payout journal "%s" has no default account.') % instance.payout_journal_id.name)

        line_vals.append((0, 0, {
            'account_id':  bank_account.id,
            'name':        f'{self.name} — Net Payout',
            'debit':       self.net_amount,
            'credit':      0.0,
            'currency_id': currency.id if currency else False,
        }))

        # DR Shopify Fees (if configured and fees > 0)
        if self.total_fees and instance.shopify_fee_account_id:
            line_vals.append((0, 0, {
                'account_id':  instance.shopify_fee_account_id.id,
                'name':        f'{self.name} — Shopify Fees',
                'debit':       self.total_fees,
                'credit':      0.0,
                'currency_id': currency.id if currency else False,
            }))

        # CR Shopify Clearing
        clearing_credit = self.net_amount + (self.total_fees if instance.shopify_fee_account_id else 0.0)
        line_vals.append((0, 0, {
            'account_id':  instance.shopify_clearing_account_id.id,
            'name':        f'{self.name} — Shopify Clearing',
            'debit':       0.0,
            'credit':      clearing_credit,
            'currency_id': currency.id if currency else False,
        }))

        move = self.env['account.move'].create({
            'journal_id':  instance.payout_journal_id.id,
            'date':        self.issued_at.date() if self.issued_at else fields.Date.today(),
            'ref':         self.name,
            'line_ids':    line_vals,
            'company_id':  company.id,
        })
        move.action_post()

        self.write({
            'odoo_move_id':      move.id,
            'reconcile_state':   'entry_created',
        })
        self.message_post(body=_('Journal entry %s created.') % move.name)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': move.id,
            'view_mode': 'form',
        }

    def action_view_journal_entry(self):
        self.ensure_one()
        if not self.odoo_move_id:
            raise UserError(_('No journal entry yet.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.odoo_move_id.id,
            'view_mode': 'form',
        }

    def action_mark_reconciled(self):
        self.write({'reconcile_state': 'reconciled'})

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def cron_import_payouts(self):
        instances = self.env['shopify.instance'].search([('state', '=', 'connected')])
        for instance in instances:
            try:
                self.import_payouts_from_shopify(instance, limit=100)
            except Exception as e:
                _logger.error('Payout import cron failed for %s: %s', instance.name, e)


class ShopifyPayoutTransaction(models.Model):
    _name = 'shopify.payout.transaction'
    _description = 'Shopify Payout Transaction'
    _order = 'payout_id, shopify_tx_id'

    payout_id         = fields.Many2one('shopify.payout', string='Payout', ondelete='cascade')
    instance_id       = fields.Many2one(related='payout_id.instance_id', store=True)
    shopify_tx_id     = fields.Char(string='Transaction ID', readonly=True)
    shopify_tx_gid    = fields.Char(string='Transaction GID', readonly=True)
    tx_type           = fields.Char(string='Type Code')
    tx_type_label     = fields.Char(string='Type')
    is_test           = fields.Boolean(string='Test', readonly=True)
    source_order_name = fields.Char(string='Order')
    gross_amount      = fields.Float(string='Gross',  digits=(16, 2))
    fee_amount        = fields.Float(string='Fee',    digits=(16, 2))
    net_amount        = fields.Float(string='Net',    digits=(16, 2))
