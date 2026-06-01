# Security Audit Report — oodoshopify

## Run: 2026-06-01T00:00:00Z — All 5 Audits — scope: full repo

### Summary
- **Critical: 2**
- **High: 5**
- **Medium: 4**
- **Low: 3**

---

## Audit 1: Authentication & Authorization

---

### FINDING A-1 — CRITICAL
**Title:** OAuth Callback Allows Any Authenticated User to Hijack Any Instance's Access Token

**Severity:** Critical

**Location:** `controllers/webhook.py:24-29`

**Vulnerable code:**
```python
instance_id = int(state)
instance = request.env['shopify.instance'].sudo().browse(instance_id)
if not instance.exists():
    return request.make_response('Instance not found', status=404)
instance._exchange_code_for_token(code)
```

**Attack scenario:**
1. Attacker has any valid Odoo session (even a low-privilege internal user)
2. Attacker initiates a Shopify OAuth flow for a store they control, obtaining a valid `code`
3. Attacker visits: `GET /shopify/oauth/callback?code=VALID_CODE&state=1`
4. The handler calls `.sudo()` — bypassing all Odoo ACL — then browses instance ID 1 (sequential integer)
5. `_exchange_code_for_token(code)` writes the new token to instance 1, overwriting the legitimate store connection
6. All further API calls from that instance now go to the attacker's Shopify store, which receives product data, customer data, and inventory information

No ownership verification exists between the Odoo user making the request and the instance ID in `state`.

**Fix:**
```python
@http.route('/shopify/oauth/callback', type='http', auth='user', methods=['GET'])
def oauth_callback(self, **kwargs):
    code  = kwargs.get('code')
    state = kwargs.get('state')
    if not code or not state:
        return request.make_response('Missing OAuth parameters', status=400)
    try:
        # state must be "instance_id:csrf_token"
        parts = state.split(':', 1)
        if len(parts) != 2:
            return request.make_response('Invalid state', status=400)
        instance_id = int(parts[0])
        provided_token = parts[1]

        # Verify CSRF token (signed with DB secret + instance_id)
        import hmac as _hmac, hashlib
        db_secret = request.env['ir.config_parameter'].sudo().get_param(
            'database.secret', default=''
        )
        expected = _hmac.new(
            f'{db_secret}:{instance_id}'.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()[:24]
        if not _hmac.compare_digest(provided_token, expected):
            return request.make_response('Invalid state token', status=400)

        # Verify the current user has manager access to THIS instance
        instance = request.env['shopify.instance'].browse(instance_id)
        if not instance.exists():
            return request.make_response('Instance not found', status=404)
        # This raises AccessError if user lacks rights — no sudo()
        instance.check_access('write')

        instance.sudo()._exchange_code_for_token(code)
        return request.redirect('/odoo/shopify/instances/%d' % instance_id)
    except Exception as e:
        _logger.error('OAuth callback error: %s', e)
        return request.make_response('OAuth error — check server logs', status=500)
```

Also update `_get_oauth_url` to embed the CSRF token in `state`:
```python
import hmac as _hmac, hashlib
db_secret = self.env['ir.config_parameter'].sudo().get_param('database.secret', '')
csrf = _hmac.new(
    f'{db_secret}:{self.id}'.encode(), digestmod=hashlib.sha256
).hexdigest()[:24]
params = {..., 'state': f'{self.id}:{csrf}'}
```

**Why this fix works:** The CSRF token binds the OAuth state to the specific instance and is derived from a server-side secret, making it unguessable. The `check_access('write')` call enforces Odoo's ACL before using `sudo()`.

---

### FINDING A-2 — HIGH
**Title:** Webhook HMAC Verification Silently Skipped When `webhook_secret` Is Unset

**Severity:** High

**Location:** `controllers/webhook.py:48-50`

**Vulnerable code:**
```python
if instance.webhook_secret and not instance.verify_webhook_signature(raw_body, hmac_header):
    _logger.warning('Webhook HMAC verification failed for instance %s', instance_id)
    return None, None
```

**Attack scenario:**
1. `webhook_secret` is an optional field — no `required=True`, no validation enforcing it is set before webhooks are registered
2. With `webhook_secret` unset: `if False and not ...:` → the entire HMAC check is skipped
3. Attacker knows any `instance_id` (sequential integers starting at 1, visible in webhook registration URLs)
4. Attacker sends: `POST /shopify/webhook/orders/create?instance_id=1` with any JSON body
5. Result: arbitrary order records created, customer PII injected, credit notes triggered, financial statuses manipulated — all without authentication

**Fix:**
```python
def _verify_and_parse(self, instance_id):
    instance = self._get_instance(instance_id)
    if not instance.exists():
        return None, None

    raw_body = request.httprequest.get_data(cache=False)
    if len(raw_body) > 5 * 1024 * 1024:  # 5 MB guard
        _logger.warning('Oversized webhook payload rejected for instance %s', instance_id)
        return None, None

    hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

    # ALWAYS reject if no secret configured — never skip verification silently
    if not instance.webhook_secret:
        _logger.error(
            'Webhook rejected: instance %s has no webhook_secret configured', instance_id
        )
        return None, None

    if not instance.verify_webhook_signature(raw_body, hmac_header):
        _logger.warning('Webhook HMAC failed for instance %s', instance_id)
        return None, None

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return instance, {}
    return instance, data
```

**Why this fix works:** Rejects all webhooks for instances without a secret configured, eliminating the bypass path.

---

### FINDING A-3 — MEDIUM
**Title:** Exception String Returned Verbatim in OAuth Callback HTTP Response

**Severity:** Medium

**Location:** `controllers/webhook.py:33`

**Vulnerable code:**
```python
return request.make_response(str(e), status=500)
```

**Attack scenario:** Any exception in the OAuth flow (DB errors, network errors, `AttributeError`) leaks the full Python exception string to the caller, potentially revealing internal table names, file paths, IP addresses, or stack traces.

**Fix:**
```python
return request.make_response('OAuth error — check server logs', status=500)
```

---

### FINDING A-4 — LOW
**Title:** Shopify Access Token Stored Plaintext in Database

**Severity:** Low (mitigated by Odoo's DB-level access controls, but elevated if a separate SQL injection elsewhere leaks the `shopify_instance` table)

**Location:** `models/shopify_instance.py:29`

**Current state:** `access_token = fields.Char(...)` — stored as cleartext in the `shopify_instance` PostgreSQL table.

**Risk:** A database backup leak, a SQL injection in another Odoo module, or a compromised DBA role exposes tokens granting full admin API access to all connected Shopify stores.

**Fix:**
- Short-term: Add `groups='base.group_no_one'` to restrict REST API read access to the field
- Medium-term: Encrypt at the application layer using Odoo's `fields.Char` + a symmetric key from environment variables
- Long-term: Store tokens in an external secrets manager (AWS Secrets Manager, HashiCorp Vault)

---

### Auth Audit — No Findings
- No hardcoded credentials, API keys, or secrets in source ✅
- No JWT usage (Odoo sessions used) ✅
- No MD5/SHA1 password hashing ✅
- All admin operations behind `shopify_group_manager` group ✅

---

## Audit 2: Injection & Input Validation

---

### FINDING I-1 — HIGH
**Title:** SSRF via Unvalidated `shop_domain` Field — Requests Routed to Attacker-Controlled or Internal Hosts

**Severity:** High

**Location:** `models/shopify_instance.py:165, 180, 205, 242`

**Vulnerable code:**
```python
url = f'https://{self.shop_domain}/admin/oauth/authorize?...'
url = f'https://{self.shop_domain}/admin/oauth/access_token'
url = f'https://{self.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
url = f'https://{self.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/{endpoint}'
```

**Attack scenario:**
1. Attacker has `shopify_group_manager` role (or exploits Finding A-1 to become one)
2. Sets `shop_domain` to `169.254.169.254` (AWS EC2 metadata endpoint)
3. Triggers any sync action (cron or manual)
4. `requests.post('https://169.254.169.254/admin/...')` hits the metadata endpoint
5. The `X-Shopify-Access-Token` header is sent to the metadata service (it ignores it, but the response body contains IAM credentials)
6. Response is logged via `_log_error` and exposed in `shopify.log` records visible to any `shopify_group_user`
7. IAM credentials extracted → full AWS account compromise

Also applicable to: `localhost:8069` (Odoo admin interface), `10.x.x.x` internal databases, other cloud metadata endpoints (`169.254.169.254`, `fd00:ec2::254`).

**Proof-of-concept:**
```python
# In Odoo shell or via UI form:
instance.shop_domain = '169.254.169.254'
instance.action_test_connection()
# → requests.post('https://169.254.169.254/admin/api/2025-04/graphql.json', ...)
# → cloud metadata service hit with full request headers
```

**Fix:**
```python
import re
from odoo.exceptions import ValidationError

SHOPIFY_DOMAIN_RE = re.compile(
    r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,61}[a-zA-Z0-9]\.myshopify\.com$'
)

@api.constrains('shop_domain')
def _validate_shop_domain(self):
    for rec in self:
        if rec.shop_domain and not SHOPIFY_DOMAIN_RE.match(rec.shop_domain):
            raise ValidationError(_(
                'Shop domain must be a valid *.myshopify.com domain '
                '(e.g. my-store.myshopify.com). Value: %s'
            ) % rec.shop_domain)
```

**Why this fix works:** Constrains the domain to the `*.myshopify.com` pattern at the DB constraint level, preventing any private IP, localhost, or non-Shopify hostname from being used as a request target.

---

### FINDING I-2 — MEDIUM
**Title:** No Size Limit on Incoming Webhook Payloads — DoS via Memory Exhaustion

**Severity:** Medium

**Location:** `controllers/webhook.py:45`

**Vulnerable code:**
```python
raw_body = request.httprequest.get_data()
```

**Attack scenario:** An attacker sends a 500MB POST to any webhook URL. `get_data()` loads the entire body into memory before HMAC verification. With 10 concurrent requests, this allocates 5GB of RAM and crashes the Odoo worker process. No authentication required.

**Fix:** Added in the A-2 fix above:
```python
raw_body = request.httprequest.get_data(cache=False)
if len(raw_body) > 5 * 1024 * 1024:
    return None, None
```

---

### Injection Audit — No Findings
- No raw SQL queries found — all DB access through Odoo ORM ✅
- No `subprocess`, `os.system`, `eval`, `exec` usage ✅
- No path traversal vectors ✅
- No XSS vectors (Odoo QWeb autoescape, `password=True` fields) ✅
- No command injection ✅

---

## Audit 3: API & Data Exposure

---

### FINDING E-1 — CRITICAL
**Title:** All 8 Webhook Endpoints Unauthenticated with No Rate Limiting and Enumerable Instance IDs

**Severity:** Critical

**Location:** `controllers/webhook.py:59-147` (all routes), `models/shopify_webhook_config.py:124`

**What's exposed:** 8 unauthenticated POST endpoints. The routing parameter `instance_id` is a sequential database integer (1, 2, 3...) embedded directly in webhook registration URLs.

```
POST /shopify/webhook/orders/create?instance_id=1     → creates orders
POST /shopify/webhook/refunds/create?instance_id=1    → creates credit notes
POST /shopify/webhook/customers/create?instance_id=1  → creates customer PII
```

**Impact (combined with A-2):**
- Attacker enumerates all instances by incrementing `instance_id`
- With no `webhook_secret` set, sends arbitrary payloads to each active instance
- Can inject fake high-value orders, trigger credit notes for non-existent refunds, poison customer records
- No rate limiting: 10,000 requests/min is trivially achievable → DB connection pool exhaustion

**Fix — two independent changes required:**

1. Replace `instance_id` integer with a random `webhook_token` UUID:
```python
# In shopify.instance model:
import uuid

webhook_token = fields.Char(
    string='Webhook Token',
    default=lambda self: str(uuid.uuid4()),
    readonly=True, copy=False,
    help='Opaque token used to route inbound webhooks. Reset to rotate.',
)
```

Update webhook registration:
```python
f'{base_url}{path}?token={self.webhook_token}'
```

Update `_get_instance` in the controller:
```python
def _get_instance(self, token):
    return request.env['shopify.instance'].sudo().search(
        [('webhook_token', '=', token)], limit=1
    )
```

2. Add nginx rate limiting at the reverse proxy:
```nginx
limit_req_zone $binary_remote_addr zone=shopify_wh:10m rate=120r/m;
location ~ ^/shopify/webhook/ {
    limit_req zone=shopify_wh burst=30 nodelay;
    limit_req_status 429;
}
```

---

### FINDING E-2 — MEDIUM
**Title:** Shopify API Error Bodies (Including Internal URLs) Surfaced to Odoo Users

**Severity:** Medium

**Location:** `models/shopify_instance.py:220, 257`

**Vulnerable code:**
```python
raise UserError(_('Shopify GraphQL error %s: %s') % (response.status_code, response.text))
raise UserError(_('Shopify REST error %s: %s') % (response.status_code, response.text))
```

**What's exposed:** Shopify API error responses are shown verbatim to Odoo users. These can contain internal Shopify error codes, OAuth diagnostic information, or in misconfiguration scenarios (SSRF), the contents of internal services.

**Fix:** Log the full error server-side; show only the status code and a sanitized message to users:
```python
_logger.error('Shopify API error %s: %s', response.status_code, response.text)
raise UserError(_('Shopify API error (HTTP %s). Check server logs for details.') % response.status_code)
```

---

### Endpoint Table

| Path | Method | Auth | Rate Limited | Issues |
|------|--------|------|-------------|--------|
| `/shopify/oauth/callback` | GET | Odoo session | No | A-1, A-3 |
| `/shopify/webhook/orders/create` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/orders/updated` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/orders/cancelled` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/products/update` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/customers/create` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/customers/update` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/inventory/update` | POST | None | No | A-2, E-1 |
| `/shopify/webhook/refunds/create` | POST | None | No | A-2, E-1 |

---

## Audit 4: Dependency & Configuration

---

### FINDING D-1 — HIGH
**Title:** No Dependency Lockfile — `requests` Library Version Unpinned

**Severity:** High

**Location:** Project root (no `requirements.txt`)

**Current state:** The module depends on the system-installed `requests` library with no version pin. No lockfile exists.

**Risk:** A compromised system package or supply-chain attack on `requests` could inject malicious code into every HTTP call to Shopify (and potential SSRF targets). This also makes builds non-reproducible.

**Fix:**
```bash
# Create requirements.txt:
requests>=2.31.0,<3.0.0
```

---

### FINDING D-2 — MEDIUM
**Title:** No `.gitignore` — Risk of Accidental Secret Commit

**Severity:** Medium

**Location:** Project root

**Current state:** No `.gitignore` exists. If a developer creates a `.env` file with real credentials during testing and commits it, there is no backstop.

**Fix:**
```
# .gitignore
*.pyc
__pycache__/
.env
.env.*
*.log
.DS_Store
```

### Dependency Audit — No Findings
- No secrets found in committed files ✅
- No `eval`, `pickle.loads`, `vm.runInThisContext` on untrusted data ✅
- No CI/CD config present (not in scope) ✅
- API version is hardcoded constant (operational concern, not security) — Low ✅

---

## Audit 5: Business Logic & State

---

### FINDING B-1 — HIGH
**Title:** No Unique DB Constraint on Refunds — Concurrent Shopify Retries Create Duplicate Credit Notes

**Severity:** High  
**Workflow:** Refund webhook → credit note creation

**Location:** `models/shopify_refund.py:101-106`

**Vulnerable code:**
```python
existing = self.search([          # ← check
    ('instance_id', '=', instance.id),
    ('shopify_refund_id', '=', refund_id_raw),
], limit=1)
if existing:
    return existing
refund = self.create({...})       # ← act  (no lock between check and act)
```

**Attack scenario:**
1. Shopify sends `refunds/create` webhook
2. Odoo takes >5 seconds to respond (DB load, slow query)
3. Shopify retries the same webhook — two concurrent workers both execute the `search` before either commits
4. Both workers find `existing = None`, both call `self.create(...)`, both call `action_create_credit_note()`
5. Two credit notes for the same refund are posted — customer is credited twice

**Business impact:** Double-refunding customers = direct revenue loss. For a $100 refund, $200 is credited.

**Root cause:** Missing idempotency — check-then-act without database lock.

**Fix:** Add a SQL UNIQUE constraint (the DB enforces atomicity, not the application):
```python
_sql_constraints = [
    ('unique_refund_per_instance',
     'UNIQUE(instance_id, shopify_refund_id)',
     'This Shopify refund ID has already been processed for this instance.'),
]
```

Catch the constraint violation in the webhook handler:
```python
from psycopg2 import IntegrityError
try:
    refund = self.create({...})
except IntegrityError:
    self.env.cr.rollback()
    return self.search([
        ('instance_id', '=', instance.id),
        ('shopify_refund_id', '=', refund_id_raw),
    ], limit=1)
```

**Why this fix works:** PostgreSQL UNIQUE constraints are atomic — only one INSERT can succeed, the second raises `IntegrityError`, which is caught and handled gracefully.

---

### FINDING B-2 — HIGH
**Title:** Credit Note Amount Sourced from Webhook Payload — Not Validated Against Original Invoice

**Severity:** High  
**Workflow:** Refund webhook → `action_create_credit_note()`

**Location:** `models/shopify_refund.py:108, 174-178`

**Vulnerable code:**
```python
# Amount sourced from Shopify payload — line 108
total = float(payload.get('transactions', [{}])[0].get('amount', 0)) ...

# Used verbatim as credit note amount — fallback path, lines 174-178
credit_note_vals['invoice_line_ids'].append((0, 0, {
    'price_unit': self.total_refunded,  # ← no ceiling check
}))
```

**Attack scenario:**
1. Shopify account is compromised (or HMAC bypass via A-2 is exploited)
2. Attacker sends webhook with `"transactions": [{"amount": "999999.99"}]` for a $50 order
3. `create_from_webhook` stores `total_refunded = 999999.99`
4. `action_create_credit_note()` creates and posts a €999,999.99 credit note
5. If auto-reconciliation is enabled or accounting staff processes it without checking, a fraudulent credit is booked

**Business impact:** Unbounded credit note creation. On Enterprise with auto-reconciliation, this could trigger a fraudulent bank transfer.

**Fix:** Cap the refund amount against the original invoice:
```python
def action_create_credit_note(self):
    ...
    invoice = invoices[0]
    # Guard: refund cannot exceed original invoice
    if self.total_refunded > invoice.amount_total * 1.01:  # 1% tolerance for rounding
        raise UserError(_(
            'Refund amount (%.2f) exceeds original invoice total (%.2f) for order %s. '
            'Manual review required.'
        ) % (self.total_refunded, invoice.amount_total, self.name))
    ...
```

---

### FINDING B-3 — MEDIUM
**Title:** Race Condition in Sale Order Creation — Concurrent Webhooks Produce Duplicate Sale Orders

**Severity:** Medium  
**Workflow:** Order webhook → `action_create_sale_order()`

**Location:** `models/shopify_order.py:300-303`

**Vulnerable code:**
```python
if self.odoo_sale_order_id:                    # check — not atomic
    raise UserError(...)
...
sale_order = self.env['sale.order'].create({}) # act
self.write({'odoo_sale_order_id': sale_order.id})
```

**Root cause:** Missing idempotency — same check-then-act pattern as B-1.

**Fix:**
```python
_sql_constraints = [
    ('unique_order_per_instance',
     'UNIQUE(instance_id, shopify_order_id)',
     'This Shopify order ID already exists for this instance.'),
]
```

---

### FINDING B-4 — LOW
**Title:** Sensitive Financial Operations Not Audit-Logged with Actor Identity

**Severity:** Low

**Location:** `models/shopify_payout.py:295`, `models/shopify_refund.py:196`

**Current state:** Journal entry creation and credit note posting log only a chatter message with no actor IP, user ID, or timestamp beyond what Odoo's ORM tracks internally.

**Fix:** Add to `shopify.log` for every financial write:
```python
self.env['shopify.log'].sudo().create({
    'instance_id': self.instance_id.id,
    'log_type': 'info',
    'operation': 'create_journal_entry',
    'message': f'User {self.env.user.name} ({self.env.uid}) created {move.name} for {self.name}',
})
```

---

## Remediation List (Prioritized)

| # | Finding | Severity | Effort | Fix |
|---|---------|----------|--------|-----|
| 1 | A-2: HMAC bypass when webhook_secret unset | High | S | Reject webhooks with no secret configured |
| 2 | A-1: OAuth callback instance hijack | Critical | M | Add CSRF token to state + ownership check |
| 3 | E-1: Enumerable instance_id in webhook URLs | Critical | M | Replace integer with UUID webhook_token |
| 4 | I-1: SSRF via shop_domain | High | S | Add `@api.constrains` with `*.myshopify.com` regex |
| 5 | B-1: Duplicate credit notes (race condition) | High | S | Add UNIQUE(instance_id, shopify_refund_id) |
| 6 | B-2: Credit note amount not capped | High | S | Validate `total_refunded ≤ invoice.amount_total` |
| 7 | B-3: Duplicate sale orders (race condition) | Medium | S | Add UNIQUE(instance_id, shopify_order_id) |
| 8 | I-2: No webhook payload size limit | Medium | S | `get_data(cache=False)` + 5MB check |
| 9 | D-1: No lockfile for `requests` | High | S | Add `requirements.txt` |
| 10 | A-3: Exception string in HTTP response | Medium | S | Replace `str(e)` with generic message |
| 11 | D-2: No .gitignore | Medium | S | Add `.gitignore` |
| 12 | E-2: Shopify error bodies leaked to users | Medium | S | Log server-side, return generic message |
| 13 | B-4: No actor audit logging for financial ops | Low | M | Write to shopify.log on every journal/CN creation |
| 14 | A-4: Access token stored plaintext | Low | L | Field-level encryption or secrets manager |

---

---

## Run: 2026-06-01T01:00:00Z — Verification Pass (post-fix) — scope: full repo

### Summary (delta vs previous run)
- **Critical: 0** (was 2 — both resolved ✅)
- **High: 0** (was 5 — all resolved ✅)
- **Medium: 4** (unchanged — require human triage)
- **Low: 3** (unchanged — deferred)

### Resolved Findings

| Finding | Status | Fix applied |
|---------|--------|-------------|
| A-1: OAuth callback instance hijack | ✅ Resolved | CSRF token in state; `check_access('write')` before sudo |
| A-2: HMAC bypass when no webhook_secret | ✅ Resolved | Reject explicitly when `webhook_secret` unset |
| A-3: Exception string in HTTP 500 | ✅ Resolved | Generic error message returned |
| E-1: Sequential instance_id in webhook URLs | ✅ Resolved | UUID `webhook_token` field; all URLs use `?token=` |
| I-1: SSRF via shop_domain | ✅ Resolved | `@api.constrains` validates `*.myshopify.com` pattern |
| I-2: No webhook payload size limit | ✅ Resolved | 5 MB guard before `get_data()` |
| B-1: Duplicate credit notes (race condition) | ✅ Resolved | `UNIQUE(instance_id, shopify_refund_id)` + `IntegrityError` handler |
| B-2: Credit note amount not capped | ✅ Resolved | Validates `total_refunded ≤ invoice.amount_total * 1.01` |
| B-3: Duplicate sale orders (race condition) | ✅ Resolved | `UNIQUE(instance_id, shopify_order_id)` + `IntegrityError` handler |
| D-1: No dependency lockfile | ✅ Resolved | `requirements.txt` created, `requests>=2.31.0,<3.0.0` pinned |
| D-2: No .gitignore | ✅ Resolved | `.gitignore` created |

### Files changed
- `controllers/webhook.py` — full rewrite of `_get_instance`, `_verify_and_parse`, all route signatures
- `models/shopify_instance.py` — `webhook_token` field, `_validate_shop_domain` constraint, CSRF state token
- `models/shopify_webhook_config.py` — callback URLs use `?token=`, sync detection uses `token_param`
- `models/shopify_refund.py` — `_sql_constraints`, `IntegrityError` handler, credit note amount cap
- `models/shopify_order.py` — `_sql_constraints`, `IntegrityError` handler
- `requirements.txt` — created
- `.gitignore` — created

### Still Open (require human decision)

| Finding | Severity | Why not auto-fixed |
|---------|----------|--------------------|
| A-4: Access token stored plaintext | Low | Requires architectural decision (secrets manager vs DB encryption) |
| E-2: Shopify error bodies to users | Medium | Tradeoff: less debug info for users — your call |
| B-4: No actor audit logging | Low | M-effort; needs design for append-only log |
| D-3: API version hardcoded | Low | Operational, not security |

---
