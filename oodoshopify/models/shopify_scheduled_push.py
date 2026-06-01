import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopifyScheduledPush(models.Model):
    """Schedule a push to Shopify to run at a future time.

    Prepare the change in Odoo now (new prices, metafields, theme settings…),
    set a go-live time, and a cron fires the matching push then — handy for
    timed sales, coordinated launches, off-peak syncs, etc.
    """
    _name = 'shopify.scheduled.push'
    _description = 'Shopify Scheduled Push'
    _inherit = ['mail.thread']
    _order = 'scheduled_date asc'

    name = fields.Char(string='Name', required=True, default='Scheduled Push')
    instance_id = fields.Many2one(
        'shopify.instance', string='Store', required=True, ondelete='cascade')
    scheduled_date = fields.Datetime(
        string='Run At', required=True, tracking=True,
        help='When the push should be sent to Shopify (server time).')

    operation = fields.Selection([
        ('push_prices', 'Push Product Prices'),
        ('push_inventory', 'Push Product Inventory'),
        ('push_metafields', 'Push Product Metafields'),
        ('push_product', 'Push Full Products'),
        ('push_theme', 'Push Theme Settings'),
    ], string='Operation', required=True, default='push_prices')

    product_ids = fields.Many2many(
        'shopify.product', string='Products',
        help='Leave empty to apply to all products of the store.')
    theme_id = fields.Many2one('shopify.theme', string='Theme')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', tracking=True)

    executed_date = fields.Datetime(string='Executed On', readonly=True)
    result_message = fields.Text(string='Result', readonly=True)

    is_product_op = fields.Boolean(compute='_compute_is_product_op')

    @api.depends('operation')
    def _compute_is_product_op(self):
        for rec in self:
            rec.is_product_op = rec.operation in (
                'push_prices', 'push_inventory', 'push_metafields', 'push_product')

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def action_schedule(self):
        for rec in self:
            if not rec.scheduled_date:
                raise UserError(_('Please set a "Run At" date first.'))
            if rec.operation == 'push_theme' and not rec.theme_id:
                raise UserError(_('Select a theme for a theme-settings push.'))
            rec.state = 'scheduled'

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_reset_draft(self):
        self.write({'state': 'draft', 'result_message': False})

    def action_run_now(self):
        for rec in self:
            rec._execute()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _targets(self):
        """Resolve the products this push applies to."""
        self.ensure_one()
        if self.product_ids:
            return self.product_ids
        return self.env['shopify.product'].search([('instance_id', '=', self.instance_id.id)])

    def _execute(self):
        self.ensure_one()
        try:
            if self.operation == 'push_prices':
                self._targets().action_push_prices()
                msg = _('Pushed prices for %d product(s).') % len(self._targets())
            elif self.operation == 'push_inventory':
                self._targets().action_sync_inventory()
                msg = _('Pushed inventory for %d product(s).') % len(self._targets())
            elif self.operation == 'push_metafields':
                self._targets().action_push_metafields()
                msg = _('Pushed metafields for %d product(s).') % len(self._targets())
            elif self.operation == 'push_product':
                self._targets().action_export_to_shopify()
                msg = _('Exported %d product(s).') % len(self._targets())
            elif self.operation == 'push_theme':
                if not self.theme_id:
                    raise UserError(_('No theme selected.'))
                self.theme_id.action_push_settings()
                msg = _('Pushed theme settings for "%s".') % self.theme_id.display_name
            else:
                raise UserError(_('Unknown operation: %s') % self.operation)

            self.write({
                'state': 'done',
                'executed_date': fields.Datetime.now(),
                'result_message': msg,
            })
            self._notify(success=True, msg=msg)
        except Exception as e:
            self.write({
                'state': 'failed',
                'executed_date': fields.Datetime.now(),
                'result_message': str(e),
            })
            self._notify(success=False, msg=str(e))
            _logger.error('Scheduled push %s failed: %s', self.id, e)

    def _notify(self, success, msg):
        self.ensure_one()
        user = self.create_uid or self.env.user
        if not user or not user.partner_id:
            return
        subject = (_('✅ Scheduled push done: %s') if success
                   else _('❌ Scheduled push failed: %s')) % self.name
        try:
            self.message_notify(
                partner_ids=user.partner_id.ids, subject=subject, body=msg)
        except Exception as e:
            _logger.warning('Scheduled-push notification failed: %s', e)

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def cron_run_due(self):
        """Execute all scheduled pushes whose time has arrived."""
        due = self.search([
            ('state', '=', 'scheduled'),
            ('scheduled_date', '<=', fields.Datetime.now()),
        ])
        for rec in due:
            rec._execute()
