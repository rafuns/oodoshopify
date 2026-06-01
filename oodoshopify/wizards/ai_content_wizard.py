import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopifyAIContentWizard(models.TransientModel):
    _name = 'shopify.ai.content.wizard'
    _description = 'AI Product Content Studio'

    product_ids = fields.Many2many('shopify.product', string='Products', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Store', required=True)

    gen_title = fields.Boolean(string='Title', default=False)
    gen_description = fields.Boolean(string='Description', default=True)
    gen_seo = fields.Boolean(string='SEO Title & Meta', default=True)
    gen_tags = fields.Boolean(string='Tags', default=False)

    tone = fields.Selection([
        ('professional', 'Professional'),
        ('friendly', 'Friendly'),
        ('luxury', 'Luxury'),
        ('playful', 'Playful'),
        ('technical', 'Technical'),
    ], default='professional', required=True)
    language = fields.Char(default='English', required=True)
    push_after = fields.Boolean(
        string='Push to Shopify after applying', default=False,
        help='Also export the updated content to Shopify immediately.')
    result_log = fields.Text(string='Result', readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'shopify.product':
            products = self.env['shopify.product'].browse(self.env.context.get('active_ids', []))
            products = products.exists()
            if products:
                res['product_ids'] = [(6, 0, products.ids)]
                res['instance_id'] = products[0].instance_id.id
        return res

    def _wanted(self):
        want = set()
        if self.gen_title:
            want.add('title')
        if self.gen_description:
            want.add('description')
        if self.gen_seo:
            want.update({'seo_title', 'seo_description'})
        if self.gen_tags:
            want.add('tags')
        return want

    def action_generate(self):
        self.ensure_one()
        want = self._wanted()
        if not want:
            raise UserError(_('Select at least one field to generate.'))

        done, failed = 0, 0
        lines = []
        for product in self.product_ids:
            try:
                result = self.instance_id.ai_generate_product_content(
                    product, want, tone=self.tone, language=self.language)
            except Exception as e:
                failed += 1
                lines.append('✗ %s: %s' % (product.name, e))
                continue

            vals = {}
            if 'title' in want and result.get('title'):
                vals['name'] = result['title']
            if 'seo_title' in want and result.get('seo_title'):
                vals['seo_title'] = result['seo_title'][:60]
            if 'seo_description' in want and result.get('seo_description'):
                vals['seo_description'] = result['seo_description'][:320]
            if 'tags' in want and result.get('tags'):
                vals['tags'] = result['tags']
            if vals:
                vals['sync_status'] = 'pending'
                product.write(vals)
            # Description goes onto the Odoo product template (source of truth for push)
            if 'description' in want and result.get('description') and product.odoo_product_id:
                product.odoo_product_id.description_sale = result['description']
            done += 1
            lines.append('✓ %s' % product.name)

        if self.push_after and done:
            self.product_ids.filtered(lambda p: p.sync_status == 'pending').action_export_to_shopify()

        self.result_log = '\n'.join(lines)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.ai.content.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': self.env.context,
        }
