import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL — Blogs, Articles, Pages (Online Store content)
# ---------------------------------------------------------------------------

QUERY_BLOGS = '''
    query GetBlogs($first: Int!, $after: String) {
        blogs(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges { node { id title handle } }
        }
    }
'''

QUERY_ARTICLES = '''
    query GetArticles($first: Int!, $after: String) {
        articles(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id title handle isPublished publishedAt
                    blog { id title }
                    author { name }
                }
            }
        }
    }
'''

QUERY_PAGES = '''
    query GetPages($first: Int!, $after: String) {
        pages(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges { node { id title handle isPublished publishedAt } }
        }
    }
'''

MUTATION_BLOG_CREATE = '''
    mutation blogCreate($blog: BlogCreateInput!) {
        blogCreate(blog: $blog) {
            blog { id title handle }
            userErrors { field message }
        }
    }
'''

MUTATION_ARTICLE_CREATE = '''
    mutation articleCreate($article: ArticleCreateInput!) {
        articleCreate(article: $article) {
            article { id title handle }
            userErrors { field message }
        }
    }
'''

MUTATION_PAGE_CREATE = '''
    mutation pageCreate($page: PageCreateInput!) {
        pageCreate(page: $page) {
            page { id title handle }
            userErrors { field message }
        }
    }
'''


class ShopifyBlog(models.Model):
    _name = 'shopify.blog'
    _description = 'Shopify Blog'
    _order = 'name'

    name = fields.Char(string='Title', required=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    shopify_blog_id = fields.Char(string='Blog ID', readonly=True)
    shopify_blog_gid = fields.Char(string='Blog GID', readonly=True)
    handle = fields.Char(string='Handle', readonly=True)
    article_ids = fields.One2many('shopify.article', 'blog_id', string='Articles')

    _unique_blog = models.Constraint(
        'UNIQUE(instance_id, shopify_blog_id)',
        'Blog already exists.',
    )

    def action_push_to_shopify(self):
        for rec in self:
            if rec.shopify_blog_gid:
                continue
            data = rec.instance_id._graphql_request(
                MUTATION_BLOG_CREATE, variables={'blog': {'title': rec.name}}
            )
            result = data.get('blogCreate', {})
            if result.get('userErrors'):
                raise UserError(_('Blog error: %s') % result['userErrors'][0]['message'])
            node = result.get('blog', {})
            gid = node.get('id', '')
            rec.write({
                'shopify_blog_gid': gid,
                'shopify_blog_id': rec.instance_id._gid_to_id(gid),
                'handle': node.get('handle', ''),
            })

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        count = 0
        while True:
            data = instance._graphql_request(QUERY_BLOGS, variables={'first': 50, 'after': cursor})
            conn = data.get('blogs', {})
            for edge in conn.get('edges', []):
                node = edge['node']
                numeric = instance._gid_to_id(node['id'])
                existing = self.search([
                    ('instance_id', '=', instance.id), ('shopify_blog_id', '=', numeric)
                ], limit=1)
                vals = {
                    'name': node.get('title', ''), 'instance_id': instance.id,
                    'shopify_blog_id': numeric, 'shopify_blog_gid': node['id'],
                    'handle': node.get('handle', ''),
                }
                (existing.write(vals) if existing else self.create(vals))
                count += 1
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
        return count


class ShopifyArticle(models.Model):
    _name = 'shopify.article'
    _description = 'Shopify Article (Blog Post)'
    _order = 'published_at desc'

    name = fields.Char(string='Title', required=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    blog_id = fields.Many2one('shopify.blog', string='Blog')
    shopify_article_id = fields.Char(string='Article ID', readonly=True)
    shopify_article_gid = fields.Char(string='Article GID', readonly=True)
    handle = fields.Char(string='Handle', readonly=True)
    author_name = fields.Char(string='Author')
    body_html = fields.Html(string='Content')
    is_published = fields.Boolean(string='Published')
    published_at = fields.Datetime(string='Published At')

    _unique_article = models.Constraint(
        'UNIQUE(instance_id, shopify_article_id)',
        'Article already exists.',
    )

    def action_push_to_shopify(self):
        for rec in self:
            if rec.shopify_article_gid or not rec.blog_id.shopify_blog_gid:
                continue
            article_input = {
                'blogId': rec.blog_id.shopify_blog_gid,
                'title': rec.name,
                'body': rec.body_html or '',
                'isPublished': rec.is_published,
            }
            if rec.author_name:
                article_input['author'] = {'name': rec.author_name}
            data = rec.instance_id._graphql_request(
                MUTATION_ARTICLE_CREATE, variables={'article': article_input}
            )
            result = data.get('articleCreate', {})
            if result.get('userErrors'):
                raise UserError(_('Article error: %s') % result['userErrors'][0]['message'])
            node = result.get('article', {})
            gid = node.get('id', '')
            rec.write({
                'shopify_article_gid': gid,
                'shopify_article_id': rec.instance_id._gid_to_id(gid),
                'handle': node.get('handle', ''),
            })

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        count = 0
        while True:
            data = instance._graphql_request(QUERY_ARTICLES, variables={'first': 50, 'after': cursor})
            conn = data.get('articles', {})
            for edge in conn.get('edges', []):
                node = edge['node']
                numeric = instance._gid_to_id(node['id'])
                blog_node = node.get('blog') or {}
                blog = self.env['shopify.blog'].search([
                    ('instance_id', '=', instance.id),
                    ('shopify_blog_id', '=', instance._gid_to_id(blog_node.get('id', ''))),
                ], limit=1) if blog_node.get('id') else False
                existing = self.search([
                    ('instance_id', '=', instance.id), ('shopify_article_id', '=', numeric)
                ], limit=1)
                vals = {
                    'name': node.get('title', ''), 'instance_id': instance.id,
                    'shopify_article_id': numeric, 'shopify_article_gid': node['id'],
                    'handle': node.get('handle', ''),
                    'author_name': (node.get('author') or {}).get('name', ''),
                    'is_published': node.get('isPublished', False),
                    'blog_id': blog.id if blog else False,
                }
                pub = (node.get('publishedAt') or '').replace('T', ' ').replace('Z', '')
                if pub:
                    vals['published_at'] = pub
                (existing.write(vals) if existing else self.create(vals))
                count += 1
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
        return count


class ShopifyPage(models.Model):
    _name = 'shopify.page'
    _description = 'Shopify Page'
    _order = 'name'

    name = fields.Char(string='Title', required=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    shopify_page_id = fields.Char(string='Page ID', readonly=True)
    shopify_page_gid = fields.Char(string='Page GID', readonly=True)
    handle = fields.Char(string='Handle', readonly=True)
    body_html = fields.Html(string='Content')
    is_published = fields.Boolean(string='Published')
    published_at = fields.Datetime(string='Published At')

    _unique_page = models.Constraint(
        'UNIQUE(instance_id, shopify_page_id)',
        'Page already exists.',
    )

    def action_push_to_shopify(self):
        for rec in self:
            if rec.shopify_page_gid:
                continue
            page_input = {
                'title': rec.name,
                'body': rec.body_html or '',
                'isPublished': rec.is_published,
            }
            data = rec.instance_id._graphql_request(
                MUTATION_PAGE_CREATE, variables={'page': page_input}
            )
            result = data.get('pageCreate', {})
            if result.get('userErrors'):
                raise UserError(_('Page error: %s') % result['userErrors'][0]['message'])
            node = result.get('page', {})
            gid = node.get('id', '')
            rec.write({
                'shopify_page_gid': gid,
                'shopify_page_id': rec.instance_id._gid_to_id(gid),
                'handle': node.get('handle', ''),
            })

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        count = 0
        while True:
            data = instance._graphql_request(QUERY_PAGES, variables={'first': 50, 'after': cursor})
            conn = data.get('pages', {})
            for edge in conn.get('edges', []):
                node = edge['node']
                numeric = instance._gid_to_id(node['id'])
                existing = self.search([
                    ('instance_id', '=', instance.id), ('shopify_page_id', '=', numeric)
                ], limit=1)
                vals = {
                    'name': node.get('title', ''), 'instance_id': instance.id,
                    'shopify_page_id': numeric, 'shopify_page_gid': node['id'],
                    'handle': node.get('handle', ''),
                    'is_published': node.get('isPublished', False),
                }
                (existing.write(vals) if existing else self.create(vals))
                count += 1
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
        return count
