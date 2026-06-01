import json
import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifyQueue(models.Model):
    _name = 'shopify.queue'
    _description = 'Shopify Sync Queue'
    _order = 'create_date desc'

    name = fields.Char(string='Queue Job', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', ondelete='cascade')
    model_name = fields.Char(string='Model')
    record_id = fields.Integer(string='Record ID')
    operation = fields.Selection([
        ('import_order', 'Import Order'),
        ('export_product', 'Export Product'),
        ('push_price', 'Push Price'),
        ('push_inventory', 'Push Inventory'),
        ('import_customer', 'Import Customer'),
        ('export_customer', 'Export Customer'),
        ('push_tracking', 'Push Tracking'),
        ('sync_theme', 'Sync Theme'),
        ('sync_locations', 'Sync Locations'),
        ('import_inventory_levels', 'Import Inventory Levels'),
        ('create_refund', 'Process Refund'),
        ('historical_import', 'Historical Import (background)'),
    ], string='Operation')

    state = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], default='pending')

    payload = fields.Text(string='Payload')
    error_message = fields.Text(string='Error')
    retry_count = fields.Integer(string='Retries', default=0)
    max_retries = fields.Integer(string='Max Retries', default=3)

    @api.model
    def cron_process_queue(self):
        """Process pending queue jobs."""
        jobs = self.search([('state', '=', 'pending'), ('retry_count', '<', 3)], limit=50)
        for job in jobs:
            job.write({'state': 'running'})
            try:
                job._process()
                job.write({'state': 'done'})
            except Exception as e:
                job.write({
                    'state': 'failed' if job.retry_count >= job.max_retries - 1 else 'pending',
                    'retry_count': job.retry_count + 1,
                    'error_message': str(e),
                })

    def _process(self):
        """Dispatch queue job to appropriate handler."""
        self.ensure_one()
        if self.operation == 'import_order':
            self.env['shopify.order'].import_orders_from_shopify(self.instance_id)
        elif self.operation == 'export_product':
            product = self.env['shopify.product'].browse(self.record_id)
            product.action_export_to_shopify()
        elif self.operation == 'push_price':
            product = self.env['shopify.product'].browse(self.record_id)
            product.action_push_prices()
        elif self.operation == 'import_customer':
            self.env['shopify.customer'].import_customers_from_shopify(self.instance_id)
        elif self.operation == 'push_tracking':
            order = self.env['shopify.order'].browse(self.record_id)
            order.action_push_tracking_to_shopify()
        elif self.operation == 'push_inventory':
            product = self.env['shopify.product'].browse(self.record_id)
            product.action_sync_inventory()
        elif self.operation == 'sync_locations':
            self.env['shopify.location'].sync_locations(self.instance_id)
        elif self.operation == 'import_inventory_levels':
            location = self.env['shopify.location'].browse(self.record_id)
            location.action_import_inventory_levels()
        elif self.operation == 'create_refund':
            payload = json.loads(self.payload or '{}')
            self.env['shopify.refund'].create_from_webhook(self.instance_id, payload)
        elif self.operation == 'historical_import':
            self._run_historical_import()

    # ------------------------------------------------------------------
    # Background historical import
    # ------------------------------------------------------------------
    @api.model
    def enqueue_historical_import(self, instance, kind, params=None):
        """Queue a large/historical import to run in the background, then poke
        the queue cron so it starts right away instead of waiting for the timer.

        :param kind: 'orders' | 'products' | 'customers'
        :param params: dict of import options (status, days_back, from_date, ...)
        """
        labels = {
            'orders': 'Orders', 'products': 'Products', 'customers': 'Customers',
            'collections': 'Collections', 'payouts': 'Payouts',
            'abandoned': 'Abandoned Checkouts', 'discounts': 'Discounts',
        }
        params = dict(params or {})

        # First call of a chunked import → create the parent Import Job that the
        # page-jobs report progress against. Chained pages carry import_job_id.
        if params.get('chunked') and not params.get('import_job_id'):
            import_job = self.env['shopify.import.job'].create({
                'instance_id': instance.id,
                'kind': kind,
                'state': 'running',
                'date_start': fields.Datetime.now(),
                'records_total': self.env['shopify.import.job'].estimate_total(
                    instance, kind, params.get('query_filter', '')),
            })
            params['import_job_id'] = import_job.id

        job = self.create({
            'name': _('Historical import: %s — %s') % (labels.get(kind, kind), instance.name),
            'instance_id': instance.id,
            'operation': 'historical_import',
            'payload': json.dumps({'kind': kind, **params}, default=str),
            'state': 'pending',
        })
        # Run the queue processor ASAP in a background worker (non-blocking).
        cron = self.env.ref('oodoshopify.cron_process_queue', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return job

    def _run_historical_import(self):
        """Execute a queued historical import using the existing import methods.

        Two modes:
        - chunked (default for wizards): import ONE page, then enqueue the next
          page as a fresh job — keeps every job short and scales to any size.
        - whole: run the full paginated import in this single job.
        """
        self.ensure_one()
        p = json.loads(self.payload or '{}')
        if p.get('chunked'):
            return self._run_historical_chunk(p)

        kind = p.get('kind')
        instance = self.instance_id

        def _dt(key):
            return fields.Datetime.to_datetime(p[key]) if p.get(key) else None

        if kind == 'orders':
            self.env['shopify.order'].import_orders_from_shopify(
                instance,
                status=p.get('status', 'any'),
                days_back=p.get('days_back', 7),
                order_ids=p.get('order_ids') or None,
                from_date=_dt('from_date'),
                to_date=_dt('to_date'),
            )
            if p.get('create_sale_orders'):
                orders = self.env['shopify.order'].search([
                    ('instance_id', '=', instance.id),
                    ('odoo_sale_order_id', '=', False),
                ])
                for order in orders:
                    try:
                        order.action_create_sale_order()
                    except Exception as e:
                        _logger.warning('Sale order creation failed for %s: %s', order.id, e)
        elif kind == 'products':
            self.env['shopify.product'].import_products_from_shopify(instance)
        elif kind == 'customers':
            self.env['shopify.customer'].import_customers_from_shopify(instance)
        elif kind == 'collections':
            self.env['shopify.collection'].import_from_shopify(instance)
        elif kind == 'payouts':
            self.env['shopify.payout'].import_payouts_from_shopify(instance)
        elif kind == 'abandoned':
            self.env['shopify.abandoned.checkout'].import_from_shopify(
                instance, days_back=p.get('days_back', 30))
        elif kind == 'discounts':
            self.env['shopify.discount'].import_from_shopify(instance)
        else:
            raise ValueError('Unknown historical import kind: %s' % kind)

    def _run_historical_chunk(self, p):
        """Import a single page, then chain the next page as a new queue job."""
        kind = p.get('kind')
        instance = self.instance_id
        cursor = p.get('cursor')
        page = p.get('page', 1)
        import_job = self.env['shopify.import.job'].browse(p['import_job_id']) \
            if p.get('import_job_id') else self.env['shopify.import.job']

        try:
            if kind == 'orders':
                count, next_cursor, has_next = self.env['shopify.order'].import_orders_page(
                    instance, p.get('query_filter', ''), cursor,
                    create_sale_orders=p.get('create_sale_orders', False))
            elif kind == 'products':
                count, next_cursor, has_next = self.env['shopify.product'].import_products_page(
                    instance, cursor)
            elif kind == 'customers':
                count, next_cursor, has_next = self.env['shopify.customer'].import_customers_page(
                    instance, cursor)
            elif kind == 'collections':
                count, next_cursor, has_next = self.env['shopify.collection'].import_collections_page(
                    instance, cursor)
            elif kind == 'payouts':
                count, next_cursor, has_next = self.env['shopify.payout'].import_payouts_page(
                    instance, cursor)
            elif kind == 'abandoned':
                checkout_model = self.env['shopify.abandoned.checkout']
                qf = p.get('query_filter') or checkout_model.build_checkouts_query_filter(
                    p.get('days_back', 30))
                count, next_cursor, has_next = checkout_model.import_checkouts_page(
                    instance, qf, cursor)
            elif kind == 'discounts':
                count, next_cursor, has_next = self.env['shopify.discount'].import_discounts_page(
                    instance, cursor)
            else:
                raise ValueError('Unknown historical import kind: %s' % kind)
        except Exception as e:
            if import_job:
                import_job.mark_failed(str(e))
            raise

        if import_job:
            import_job.add_progress(count)

        _logger.info('Historical chunk (%s) page %d: imported %d records for %s',
                     kind, page, count, instance.name)

        # Chain the next page as a fresh job so each stays small.
        if has_next and next_cursor:
            self.enqueue_historical_import(
                instance, kind, {**p, 'cursor': next_cursor, 'page': page + 1})
        elif import_job:
            import_job.mark_done()

    def action_retry(self):
        self.write({'state': 'pending', 'retry_count': 0, 'error_message': False})
