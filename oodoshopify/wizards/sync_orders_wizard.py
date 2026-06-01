from odoo import models, fields, api, _


class SyncOrdersWizard(models.TransientModel):
    _name = 'shopify.sync.orders.wizard'
    _description = 'Sync Orders Wizard'

    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True)
    filter_type = fields.Selection([
        ('days_back',   'Last N days'),
        ('date_range',  'Date range'),
        ('ids',         'Specific order IDs'),
    ], string='Filter By', default='days_back')
    days_back = fields.Integer(string='Import Last N Days', default=7)
    from_date = fields.Datetime(string='From Date')
    to_date   = fields.Datetime(string='To Date')
    order_ids_text = fields.Char(
        string='Order IDs (comma-separated)',
        help='e.g. 5001234567890, 5001234567891',
    )
    order_status = fields.Selection([
        ('any', 'Any'),
        ('open', 'Open'),
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled'),
    ], string='Order Status', default='any')
    create_sale_orders = fields.Boolean(string='Auto-create Odoo Sale Orders', default=False)
    run_in_background = fields.Boolean(
        string='Run in background',
        help='Recommended for large historical imports. The import runs as a '
             'background job so the screen does not freeze — track progress in '
             'Monitoring → Sync Queue.',
    )

    def action_confirm(self):
        order_ids = None
        from_date = None
        to_date   = None
        if self.filter_type == 'ids' and self.order_ids_text:
            order_ids = [int(i.strip()) for i in self.order_ids_text.split(',') if i.strip().isdigit()]
        elif self.filter_type == 'date_range':
            from_date = self.from_date
            to_date   = self.to_date

        # Background path — chunked (one page per job), non-blocking & scalable.
        if self.run_in_background:
            query_filter = self.env['shopify.order'].build_orders_query_filter(
                status=self.order_status, days_back=self.days_back,
                order_ids=order_ids, from_date=from_date, to_date=to_date)
            self.env['shopify.queue'].enqueue_historical_import(
                self.instance_id, 'orders', {
                    'chunked': True,
                    'query_filter': query_filter,
                    'create_sale_orders': self.create_sale_orders,
                })
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Import started'),
                    'message': _('Orders are importing in the background. '
                                 'Track progress in Monitoring → Sync Queue.'),
                    'type': 'success',
                    'next': {'type': 'ir.actions.act_window_close'},
                },
            }

        count = self.env['shopify.order'].import_orders_from_shopify(
            self.instance_id,
            status=self.order_status,
            days_back=self.days_back,
            order_ids=order_ids,
            from_date=from_date,
            to_date=to_date,
        )
        if self.create_sale_orders:
            orders = self.env['shopify.order'].search([
                ('instance_id', '=', self.instance_id.id),
                ('odoo_sale_order_id', '=', False),
            ])
            for order in orders:
                try:
                    order.action_create_sale_order()
                except Exception:
                    pass

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Orders Synced'),
                'message': _('Imported %d orders from Shopify.') % count,
                'type': 'success',
            },
        }
