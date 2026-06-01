import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class ShopifyScheduledSale(models.Model):
    """A timed sale: drop prices at a start time, auto-restore them at the end.

    On activation we snapshot every variant's current price + compare-at price,
    set the sale price (with the original kept as Shopify's strikethrough
    compareAtPrice), and at the end time restore the snapshot exactly.
    """
    _name = 'shopify.scheduled.sale'
    _description = 'Shopify Scheduled Sale'
    _inherit = ['mail.thread']
    _order = 'start_date asc'

    name = fields.Char(string='Name', required=True, default='Sale')
    instance_id = fields.Many2one(
        'shopify.instance', string='Store', required=True, ondelete='cascade')
    product_ids = fields.Many2many(
        'shopify.product', string='Products', required=True,
        help='Products that go on sale.')

    discount_type = fields.Selection([
        ('percent', 'Percent off'),
        ('amount', 'Amount off'),
        ('fixed', 'Fixed sale price'),
    ], string='Discount', default='percent', required=True)
    discount_value = fields.Float(string='Value', required=True)

    start_date = fields.Datetime(string='Starts', required=True, tracking=True)
    end_date = fields.Datetime(
        string='Ends', tracking=True,
        help='When the original prices are restored. Leave empty for a permanent drop.')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('active', 'Active'),
        ('ended', 'Ended'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', tracking=True)

    line_ids = fields.One2many(
        'shopify.scheduled.sale.line', 'sale_id', string='Price Snapshots', readonly=True)
    result_message = fields.Text(string='Last Result', readonly=True)

    @api.constrains('discount_type', 'discount_value')
    def _check_value(self):
        for rec in self:
            if rec.discount_type == 'percent' and not (0 < rec.discount_value < 100):
                raise ValidationError(_('Percent off must be between 0 and 100.'))
            if rec.discount_value < 0:
                raise ValidationError(_('Discount value cannot be negative.'))

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        for rec in self:
            if rec.end_date and rec.start_date and rec.end_date <= rec.start_date:
                raise ValidationError(_('End date must be after the start date.'))

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def action_schedule(self):
        for rec in self:
            if not rec.product_ids:
                raise UserError(_('Add at least one product to the sale.'))
            rec.state = 'scheduled'

    def action_cancel(self):
        for rec in self:
            # If a cancelled sale was already active, restore prices first.
            if rec.state == 'active':
                rec._end()
            rec.state = 'cancelled'

    def action_activate_now(self):
        for rec in self:
            rec._activate()

    def action_end_now(self):
        for rec in self:
            rec._end()

    # ------------------------------------------------------------------
    # Pricing maths
    # ------------------------------------------------------------------
    def _sale_price(self, original):
        self.ensure_one()
        if self.discount_type == 'percent':
            return round(original * (1 - self.discount_value / 100.0), 2)
        if self.discount_type == 'amount':
            return round(max(0.0, original - self.discount_value), 2)
        return round(self.discount_value, 2)  # fixed

    # ------------------------------------------------------------------
    # Apply / revert
    # ------------------------------------------------------------------
    def _activate(self):
        self.ensure_one()
        try:
            self.line_ids.unlink()
            for product in self.product_ids:
                price_map = {}
                for variant in product.variant_ids:
                    if not variant.shopify_variant_gid:
                        continue
                    original = variant.price
                    sale_price = self._sale_price(original)
                    if sale_price >= original:
                        continue  # nothing to discount
                    self.env['shopify.scheduled.sale.line'].create({
                        'sale_id': self.id,
                        'variant_id': variant.id,
                        'original_price': original,
                        'original_compare_at': variant.compare_at_price,
                        'sale_price': sale_price,
                    })
                    price_map[variant.shopify_variant_gid] = {
                        'price': '{:.2f}'.format(sale_price),
                        # keep the original as the strikethrough price
                        'compareAtPrice': '{:.2f}'.format(original),
                    }
                if price_map:
                    product.push_variant_price_map(price_map)

            self.write({
                'state': 'active',
                'result_message': _('Sale applied to %d variant(s).') % len(self.line_ids),
            })
            self._notify(_('🏷️ Sale "%s" is now live') % self.name)
        except Exception as e:
            self.write({'state': 'failed', 'result_message': str(e)})
            self._notify(_('❌ Sale "%s" failed to start: %s') % (self.name, e))
            _logger.error('Scheduled sale %s activate failed: %s', self.id, e)

    def _end(self):
        self.ensure_one()
        try:
            by_product = {}
            for line in self.line_ids:
                product = line.variant_id.shopify_product_id
                by_product.setdefault(product, {})[line.variant_id.shopify_variant_gid] = {
                    'price': '{:.2f}'.format(line.original_price),
                    'compareAtPrice': ('{:.2f}'.format(line.original_compare_at)
                                       if line.original_compare_at else None),
                }
            for product, price_map in by_product.items():
                product.push_variant_price_map(price_map)

            self.write({
                'state': 'ended',
                'result_message': _('Original prices restored for %d variant(s).') % len(self.line_ids),
            })
            self._notify(_('Sale "%s" ended — prices restored') % self.name)
        except Exception as e:
            self.write({'state': 'failed', 'result_message': str(e)})
            self._notify(_('❌ Sale "%s" failed to revert: %s') % (self.name, e))
            _logger.error('Scheduled sale %s end failed: %s', self.id, e)

    def _notify(self, message):
        self.ensure_one()
        user = self.create_uid or self.env.user
        if user and user.partner_id:
            try:
                self.message_notify(partner_ids=user.partner_id.ids,
                                    subject=message, body=message)
            except Exception as e:
                _logger.warning('Sale notification failed: %s', e)

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def cron_run_due(self):
        now = fields.Datetime.now()
        # Start scheduled sales whose start time has arrived
        for sale in self.search([('state', '=', 'scheduled'), ('start_date', '<=', now)]):
            sale._activate()
        # End active sales whose end time has passed
        for sale in self.search([('state', '=', 'active'),
                                 ('end_date', '!=', False), ('end_date', '<=', now)]):
            sale._end()


class ShopifyScheduledSaleLine(models.Model):
    _name = 'shopify.scheduled.sale.line'
    _description = 'Shopify Scheduled Sale Price Snapshot'

    sale_id = fields.Many2one('shopify.scheduled.sale', required=True, ondelete='cascade')
    variant_id = fields.Many2one('shopify.product.variant', required=True, ondelete='cascade')
    original_price = fields.Float(string='Original Price')
    original_compare_at = fields.Float(string='Original Compare-At')
    sale_price = fields.Float(string='Sale Price')
