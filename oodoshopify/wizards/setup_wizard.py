"""
Guided 8-step setup wizard for a new Shopify instance.
Walks users through credentials → OAuth → test → data import
settings → product matching → webhooks → workflow → go live.
"""
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ShopifySetupWizard(models.TransientModel):
    _name = 'shopify.setup.wizard'
    _description = 'Shopify Setup Wizard'

    # ── Progress ──────────────────────────────────────────────────────────────
    step = fields.Integer(default=1)
    total_steps = fields.Integer(default=8, readonly=True)
    progress = fields.Float(compute='_compute_progress')
    step_label = fields.Char(compute='_compute_step_label')

    @api.depends('step')
    def _compute_progress(self):
        for rec in self:
            rec.progress = round((rec.step / rec.total_steps) * 100)

    @api.depends('step')
    def _compute_step_label(self):
        labels = {
            1: 'Step 1 of 8 — Store Credentials',
            2: 'Step 2 of 8 — Connect via OAuth',
            3: 'Step 3 of 8 — Test Connection',
            4: 'Step 4 of 8 — Data Import Settings',
            5: 'Step 5 of 8 — Product Matching',
            6: 'Step 6 of 8 — Register Webhooks',
            7: 'Step 7 of 8 — Order Workflow',
            8: 'Step 8 of 8 — Review & Go Live',
        }
        for rec in self:
            rec.step_label = labels.get(rec.step, '')

    # ── Step 1: Credentials ───────────────────────────────────────────────────
    instance_id = fields.Many2one('shopify.instance', string='Existing Instance',
                                   help='Leave empty to create a new one.')
    instance_name = fields.Char(string='Store Name')
    shop_domain    = fields.Char(string='Shop Domain')
    api_key        = fields.Char(string='API Key')
    api_secret     = fields.Char(string='API Secret')

    # ── Step 2: OAuth ─────────────────────────────────────────────────────────
    oauth_done = fields.Boolean(string='OAuth Connected')

    # ── Step 3: Test ─────────────────────────────────────────────────────────
    connection_status = fields.Char(string='Connection Status', readonly=True)
    shop_name_detected = fields.Char(string='Detected Store Name', readonly=True)
    currency_detected  = fields.Char(string='Detected Currency', readonly=True)

    # ── Step 4: Data import settings ──────────────────────────────────────────
    import_products  = fields.Boolean(string='Import Products', default=True)
    import_customers = fields.Boolean(string='Import Customers', default=True)
    import_orders    = fields.Boolean(string='Import Orders', default=True)
    import_days_back = fields.Integer(string='Import Last N Days of Orders', default=30)
    new_product_status = fields.Selection([
        ('ACTIVE', 'Active — publish immediately'),
        ('DRAFT',  'Draft — review before publishing'),
    ], default='ACTIVE', string='New Product Default Status')
    fallback_customer_id = fields.Many2one('res.partner', string='Fallback Customer for Guest Orders')

    # ── Step 5: Product matching ──────────────────────────────────────────────
    product_matching_strategy = fields.Selection([
        ('sku',              'SKU only'),
        ('barcode',          'Barcode only'),
        ('sku_then_barcode', 'SKU first, then Barcode'),
    ], default='sku', string='Match Products by')
    inventory_qty_type = fields.Selection([
        ('free',       'Free to Use'),
        ('on_hand',    'On Hand'),
        ('forecasted', 'Forecasted'),
    ], default='free', string='Push This Qty to Shopify')

    # ── Step 6: Webhooks ──────────────────────────────────────────────────────
    webhooks_registered = fields.Boolean(string='Webhooks Registered')
    webhook_count = fields.Integer(string='Webhooks Count', readonly=True)

    # ── Step 7: Workflow ──────────────────────────────────────────────────────
    auto_confirm_order   = fields.Boolean(string='Auto-Confirm Sale Order', default=True)
    auto_create_invoice  = fields.Boolean(string='Auto-Create Invoice', default=False)
    auto_register_payment = fields.Boolean(string='Auto-Register Payment', default=False)
    auto_mark_paid       = fields.Boolean(string='Auto Mark Paid on Shopify', default=False)

    # ── Step 8: Summary ───────────────────────────────────────────────────────
    setup_summary = fields.Text(string='Setup Summary', readonly=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Navigation
    # ─────────────────────────────────────────────────────────────────────────

    def action_next(self):
        self.ensure_one()
        if self.step == 1:
            self._step1_save_credentials()
        elif self.step == 3:
            self._step3_test_connection()
        elif self.step == 6:
            self._step6_register_webhooks()
        elif self.step == 7:
            self._step7_save_workflow()
        elif self.step == 8:
            return self._step8_finish()

        self.step = min(self.step + 1, self.total_steps)
        return self._reopen()

    def action_back(self):
        self.step = max(self.step - 1, 1)
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Step implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _step1_save_credentials(self):
        """Create or update the ShopifyInstance with provided credentials."""
        if not self.shop_domain or not self.api_key or not self.api_secret:
            raise UserError(_('Please fill in Shop Domain, API Key, and API Secret.'))
        if self.instance_id:
            self.instance_id.write({
                'shop_domain': self.shop_domain,
                'api_key': self.api_key,
                'api_secret': self.api_secret,
            })
        else:
            instance = self.env['shopify.instance'].create({
                'name': self.instance_name or self.shop_domain,
                'shop_domain': self.shop_domain,
                'api_key': self.api_key,
                'api_secret': self.api_secret,
            })
            self.instance_id = instance

    def action_connect_oauth(self):
        """Launch Shopify OAuth flow (Step 2)."""
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_('Save credentials first (Step 1).'))
        result = self.instance_id.action_connect_oauth()
        self.oauth_done = True
        return result

    def _step3_test_connection(self):
        """Test the API connection and detect store details."""
        if not self.instance_id:
            raise UserError(_('No instance configured.'))
        try:
            result = self.instance_id.action_test_connection()
            self.connection_status = 'Connected ✓'
            self.shop_name_detected = self.instance_id.name
            self.currency_detected = self.instance_id.currency_id.name if self.instance_id.currency_id else '?'
        except Exception as e:
            self.connection_status = f'Failed: {e}'
            raise

    def _step4_apply_settings(self):
        """Apply data import settings to the instance."""
        self.instance_id.write({
            'sync_products':       self.import_products,
            'sync_customers':      self.import_customers,
            'sync_orders':         self.import_orders,
            'new_product_status':  self.new_product_status,
            'fallback_customer_id': self.fallback_customer_id.id if self.fallback_customer_id else False,
        })

    def _step5_apply_matching(self):
        self.instance_id.write({
            'product_matching_strategy': self.product_matching_strategy,
            'inventory_qty_type':        self.inventory_qty_type,
        })

    def _step6_register_webhooks(self):
        self.ensure_one()
        if not self.instance_id:
            return
        self.instance_id.action_register_webhooks()
        self.webhooks_registered = True
        self.webhook_count = len(self.instance_id.webhook_config_ids.filtered(
            lambda c: c.state == 'active'
        ))

    def _step7_save_workflow(self):
        """Create or update the order workflow for this instance."""
        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', self.instance_id.id)], limit=1
        )
        vals = {
            'auto_confirm_order':    self.auto_confirm_order,
            'auto_create_invoice':   self.auto_create_invoice,
            'auto_register_payment': self.auto_register_payment,
            'auto_mark_paid':        self.auto_mark_paid,
        }
        if workflow:
            workflow.write(vals)
        else:
            self.env['shopify.workflow'].create({'instance_id': self.instance_id.id, **vals})

    def _step8_finish(self):
        """Apply all remaining settings and show the instance."""
        self._step4_apply_settings()
        self._step5_apply_matching()

        summary_lines = [
            f'Store: {self.instance_id.name} ({self.instance_id.shop_domain})',
            f'Import products: {self.import_products}, customers: {self.import_customers}, orders: {self.import_orders}',
            f'Order history: last {self.import_days_back} days',
            f'Product matching: {self.product_matching_strategy}',
            f'Webhooks: {self.webhook_count} active',
            f'Auto-confirm orders: {self.auto_confirm_order}',
        ]
        self.setup_summary = '\n'.join(summary_lines)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.instance',
            'res_id': self.instance_id.id,
            'view_mode': 'form',
        }
