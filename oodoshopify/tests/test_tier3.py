"""Tests for Tier 3 — content (blog/article/page), files, segments, companies."""
from .common import ShopifyTestBase, SAMPLE_COMPANY_NODE


class TestBlogArticle(ShopifyTestBase):

    def test_blog_import(self):
        self.mock_gql.return_value = {
            'blogs': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                      'edges': [{'node': {'id': 'gid://shopify/Blog/1', 'title': 'News', 'handle': 'news'}}]},
        }
        count = self.env['shopify.blog'].import_from_shopify(self.instance)
        self.assertEqual(count, 1)
        self.assertTrue(self.env['shopify.blog'].search([('shopify_blog_id', '=', '1')]))

    def test_article_push_requires_blog_gid(self):
        blog = self.env['shopify.blog'].create({'name': 'B', 'instance_id': self.instance.id})
        article = self.env['shopify.article'].create({
            'name': 'Post', 'instance_id': self.instance.id, 'blog_id': blog.id,
        })
        # No blog GID → push is a no-op, should not raise
        article.action_push_to_shopify()
        self.assertFalse(article.shopify_article_gid)

    def test_blog_push(self):
        blog = self.env['shopify.blog'].create({'name': 'Stories', 'instance_id': self.instance.id})
        self.mock_gql.return_value = {
            'blogCreate': {'blog': {'id': 'gid://shopify/Blog/50', 'title': 'Stories', 'handle': 'stories'},
                           'userErrors': []},
        }
        blog.action_push_to_shopify()
        self.assertEqual(blog.shopify_blog_id, '50')


class TestPage(ShopifyTestBase):

    def test_page_push(self):
        page = self.env['shopify.page'].create({
            'name': 'About Us', 'instance_id': self.instance.id, 'body_html': '<p>Hi</p>',
        })
        self.mock_gql.return_value = {
            'pageCreate': {'page': {'id': 'gid://shopify/Page/9', 'title': 'About Us', 'handle': 'about-us'},
                           'userErrors': []},
        }
        page.action_push_to_shopify()
        self.assertEqual(page.shopify_page_id, '9')


class TestFile(ShopifyTestBase):

    def test_upload_requires_source(self):
        from odoo.exceptions import UserError
        f = self.env['shopify.file'].create({'name': 'img', 'instance_id': self.instance.id})
        with self.assertRaises(UserError):
            f.action_upload()

    def test_upload(self):
        f = self.env['shopify.file'].create({
            'name': 'logo', 'instance_id': self.instance.id,
            'source_url': 'https://example.com/logo.png',
        })
        self.mock_gql.return_value = {
            'fileCreate': {'files': [{'id': 'gid://shopify/MediaImage/3', 'fileStatus': 'READY',
                                      'image': {'url': 'https://cdn.shopify.com/logo.png'}}],
                           'userErrors': []},
        }
        f.action_upload()
        self.assertEqual(f.shopify_file_id, '3')
        self.assertEqual(f.file_status, 'READY')


class TestSegment(ShopifyTestBase):

    def test_push(self):
        seg = self.env['shopify.segment'].create({
            'name': 'VIPs', 'instance_id': self.instance.id, 'query': 'amount_spent > 1000',
        })
        self.mock_gql.return_value = {
            'segmentCreate': {'segment': {'id': 'gid://shopify/Segment/12', 'name': 'VIPs',
                                          'query': 'amount_spent > 1000'}, 'userErrors': []},
        }
        seg.action_push_to_shopify()
        self.assertEqual(seg.shopify_segment_id, '12')


class TestCompany(ShopifyTestBase):

    def test_import_creates_company_and_partner(self):
        self.mock_gql.return_value = {
            'companies': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                          'edges': [{'node': SAMPLE_COMPANY_NODE}]},
        }
        count = self.env['shopify.company'].import_from_shopify(self.instance)
        self.assertEqual(count, 1)
        company = self.env['shopify.company'].search([('shopify_company_id', '=', '8001')])
        self.assertEqual(company.name, 'Acme Wholesale Ltd')
        self.assertAlmostEqual(company.total_spent, 15000.0)
        # Auto-linked Odoo partner
        self.assertTrue(company.odoo_partner_id)
        self.assertTrue(company.odoo_partner_id.is_company)


class TestTranslation(ShopifyTestBase):

    def test_push_translation(self):
        tr = self.env['shopify.translation'].create({
            'instance_id': self.instance.id, 'resource_type': 'PRODUCT',
            'resource_gid': 'gid://shopify/Product/123', 'locale': 'fr',
            'key': 'title', 'value': 'Chemise',
        })
        self.mock_gql.return_value = {
            'translationsRegister': {'translations': [{'key': 'title', 'locale': 'fr', 'value': 'Chemise'}],
                                     'userErrors': []},
        }
        tr.action_push_translation()  # should not raise
        self.assertTrue(self.mock_gql.called)
