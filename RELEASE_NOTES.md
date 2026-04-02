# v1.8.0-beta Release Notes

This beta release brings major new capabilities to Clawith, including a fully redesigned identity system, AgentBay cloud computer visual control, a new email notification system, expanded Feishu integrations, platform-wide analytics, and many UX improvements.

---

## What's New

### 1. AgentBay: Cloud Computer Visual Control

A major leap in agent-computer interaction:

- **Live Preview Panel** — Watch your agent in real time via a draggable sidebar that streams screenshots of the cloud browser or desktop as the agent operates.
- **Vision Injection** — Browser and desktop screenshots are captured ephemerally (in-memory only, no disk writes) and injected into the LLM as multimodal input, enabling visual reasoning about on-screen state.
- **Take Control / Save Login State** — Users can seamlessly take over the agent's remote session with keyboard and mouse for manual steps (e.g., CAPTCHA, QR code scanning). Cookie-based login state can be saved and reused by the agent.
- **16+ New AgentBay Tools** — Browser extract, observe, shell commands, OTP-aware keyboard input, `browser_login`, `agentbay_browser_screenshot`, and more.
- **Windows Cloud Desktop Support** — Full Windows OS support for AgentBay cloud computer sessions.

### 2. Unified Identity System & Multi-Tenant Auth

A complete redesign of the authentication and user management architecture:

- **Global Identity (cross-tenant)** — A new `identities` table unifies credentials across organizations. One email/password works across all companies a user belongs to.
- **Tenant Switcher Modal** — Redesigned company switcher with inline join/create flow.
- **Invitation Flow Fix** — When an existing user clicks an invitation link, they are now automatically joined to the invited company after logging in.
- **SSO Toggle** — Per-channel SSO login enable/disable, with auto-detected subdomain and callback URL generation.

### 3. Platform Email System

- Password recovery via email (full reset flow with branded templates).
- Broadcast notification emails to all platform users.
- In-app SMTP configuration UI under **Platform Settings → Email** — no `.env` restart required.
- Test email button and customizable email templates.
- Auto-verify email and activate users when SMTP is not configured.

### 4. Feishu Integration Expansion

- **Bitable (多维表格)** — List tables/fields, query/create/update/delete records, create new Bitable apps. Returns clickable links in chat.
- **Drive Tools** — `feishu_drive_share` (renamed and enhanced) + new `feishu_drive_delete` for automated file permission management and cleanup.
- **Calendar & Approval** — Full integration of Feishu Calendar scheduling and Approval submission tools.
- **403 Permission Guidance** — When a Feishu API call fails due to missing permissions, the agent now provides detailed diagnostic guidance inline.

### 5. Platform Admin Dashboard & Analytics

- **Enhanced Metrics** — DAU/WAU/MAU, session counts, user retention, channel distribution, tool category breakdowns, and churn warnings.
- **Token Usage Leaderboards** — Per-agent, per-tenant daily/monthly token spend tracking backed by a new `daily_token_usage` table.
- **Org Admin Email** — Platform admins can view and contact organization admin email addresses.

### 6. LLM Engine & Tool Improvements

- **Model Toggle** — Enable/disable individual LLM models in Company Settings; disabled models are filtered from dropdowns and the runtime auto-falls back.
- **Configurable LLM Request Timeout** — New `request_timeout` setting for local/slow models.
- **Anthropic Prompt Cache Optimization** — Static and dynamic context are split to maximize cache hit rates; detailed cache metrics are logged.
- **Image Generation (Multi-Provider)** — `generate_image` tool now supports SiliconFlow, OpenAI DALL-E, and Google Vertex AI as separate configurable providers.
- **Unified Tool Configuration** — Secure API key management with Agent→Company priority inheritance and schema-aware decryption.
- **ClawHub Integration** — `search_clawhub` and `install_skill` are now seeded as built-in tools, allowing agents to self-extend from the community marketplace.
- **Transliteration Search** — Enterprise member search now supports pinyin input for Chinese names.

### 7. UX & UI Improvements

- **Chat shortcut** — `Enter` to send, `Shift+Enter` for newline.
- **Copy Button** — One-click copy on all chat messages.
- **User Email Update** — Users can change their own email address from Account Settings.
- **org_admin Promotion/Demotion** — With last-admin protection to prevent lockout.
- **Collapsible Session List** — Sidebars auto-collapse when the Live Preview panel is active.
- **Model field** — Replaced model dropdown with free-text input in LLM settings for better compatibility.

---

## Upgrade Guide

### Who this applies to

- Upgrading from **v1.7.2** (standard release)
- Upgrading from an **intermediate post-v1.7.2 state** (same steps, lower risk)

### Important Notes Before Upgrading

> **Always back up your database before upgrading.**

**1. Database Migrations (12 total)**

This release includes 12 Alembic migrations. The most significant is `user_refactor_v1`, which introduces a global `identities` table and migrates user credentials from the `users` table. All migrations are idempotent — if your instance already ran some of them, they will be skipped safely.

**2. Invitation Codes Reset**

The `multi_tenant_registration` migration adds `tenant_id` to invitation codes and clears all legacy codes that lack this field. **Existing invitation codes will be invalidated.** Regenerate them from Company Settings after upgrading.

**3. SMTP Configuration (for post-v1.7.2 intermediate versions only)**

If you previously configured `SYSTEM_SMTP_*` environment variables, these are now **ignored**. After upgrading, go to **Platform Settings → Email** and re-enter your SMTP credentials via the UI. Users upgrading from v1.7.2 are unaffected (email was not available in that version).

**4. New Optional Environment Variables**

```bash
# Public URL used in email links and webhook callbacks.
# Recommended for production; auto-detected from request host if not set.
PUBLIC_BASE_URL=https://your-domain.com

# Password reset token lifetime in minutes (default: 30)
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES=30
```

**5. New Python Dependencies**

Three new packages are required (automatically installed by Docker build):
- `wuying-agentbay-sdk >= 0.18.0`
- `pypinyin >= 0.52.0`
- `Pillow >= 10.0.0`

---

### Option A: Docker Deployment (Recommended)

```bash
# 1. Back up your database
docker exec clawith-postgres-1 pg_dump -U clawith clawith > backup_$(date +%Y%m%d).sql

# 2. Pull the latest code
cd <your-clawith-directory>
git pull origin main

# 3. Rebuild and restart all containers
docker compose down
docker compose up -d --build

# 4. Check migration logs
docker logs clawith-backend-1 2>&1 | head -80
```

**Post-upgrade checklist:**
- [ ] Go to **Platform Settings → Email** and configure SMTP (if you want password recovery / broadcast emails)
- [ ] Regenerate invitation codes in Company Settings (old codes are cleared)
- [ ] Optionally set `PUBLIC_BASE_URL` in your `.env`

---

### Option B: Source Deployment

```bash
# 1. Back up your database
pg_dump -U clawith clawith > backup_$(date +%Y%m%d).sql

# 2. Pull the latest code
cd <your-clawith-directory>
git pull origin main

# 3. Update Python dependencies
cd backend && pip install -e ".[dev]"

# 4. Run database migrations
alembic upgrade head

# 5. Build the frontend
cd ../frontend && npm install && npm run build

# 6. Restart your backend service
```

**Post-upgrade checklist:** Same as Option A.

---

### Rollback

If something goes wrong, restore from your SQL backup:

```bash
# Docker deployment
docker compose down
docker exec clawith-postgres-1 psql -U clawith -c "DROP DATABASE clawith; CREATE DATABASE clawith;"
docker exec -i clawith-postgres-1 psql -U clawith clawith < backup_YYYYMMDD.sql
git checkout v1.7.2
docker compose up -d --build
```

> We recommend SQL restore over `alembic downgrade`, as down migrations have not been fully tested.
