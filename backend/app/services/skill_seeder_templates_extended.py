"""Extended builtin skill templates."""

BUILTIN_SKILLS_EXTENDED = [
    # ─── Content Research Writer ──────────────────
    {
        "name": "Content Research Writer",
        "description": "Assists in writing high-quality content by conducting research, adding citations, improving hooks, iterating on outlines, and providing real-time section feedback",
        "category": "writing",
        "icon": "✍️",
        "folder_name": "content-research-writer",
        "files": [],  # populated at runtime
    },
    # ─── MCP Tool Installer (mandatory default) ──────────────
    {
        "name": "MCP Tool Installer",
        "description": "Guide users through discovering, configuring, and installing MCP tools directly in chat — no Settings page required",
        "category": "development",
        "icon": "🔌",
        "folder_name": "mcp-installer",
        "is_default": True,
        "files": [],  # populated at runtime from agent_template/skills/mcp-installer/SKILL.md
    },
    # ─── Market Data (trading agents) ──────────────
    {
        "name": "Market Data",
        "description": "Fetch stock quotes, OHLCV history, and fundamentals via a remote MCP server. Use when a trading agent needs price/financial data on US equities.",
        "category": "trading",
        "icon": "MD",
        "folder_name": "market-data",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Market Data
description: Stock quotes, OHLCV history, and fundamentals for US equities via Smithery MCP
---

# Market Data

## When to Use This Skill

Use when a trading agent needs:
- Real-time or historical price data on US equities (NYSE / NASDAQ)
- Financial statements (income, balance sheet, cash flow)
- Pre-computed technical indicators (RSI, MACD, Bollinger Bands, SMA, EMA, ADX, etc.)
- Quarterly EPS actuals, estimates, and surprises

**Scope (v1)**: US-listed equities only. **Not yet covered**: futures (CL=F, GC=F, ES=F), forex, crypto, international stocks. For these, fall back to `web-research`.

---

## Step-by-Step Protocol

### Step 1 — Check if Shibui Finance MCP is already installed

Look at your tool list. If you have `unlock_financial_analysis` and `stock_data_query` tools, skip to Step 3.

### Step 2 — Install via MCP_INSTALLER

Use the `mcp-installer` skill to install Shibui Finance (free, no API key, no per-call cost):

```
import_mcp_server(
  server_id="shibui/finance",
  config={"smithery_api_key": "<key>"}  # only on first import; reused after
)
```

If the user has not yet provided a Smithery API key, the `mcp-installer` skill explains how to register and obtain one.

### Step 3 — Activate the data session

The Shibui MCP requires a one-time activation per session before SQL queries work:

```
unlock_financial_analysis(...)
```

This returns an access token automatically managed by the MCP — you don't need to pass it in subsequent calls.

### Step 4 — Query data

The primary tool is `stock_data_query`, which takes natural-language prompts or SQL. Examples:

#### Get latest quote
```
stock_data_query(query="Get the most recent close price, daily change %, and volume for AAPL")
```

#### Get OHLCV history
```
stock_data_query(query="Daily OHLCV for TSLA over the past 90 trading days")
```

#### Get fundamentals
```
stock_data_query(query="Latest annual income statement and balance sheet for MSFT, with key ratios PE PB ROE")
```

#### Get pre-computed indicator
```
stock_data_query(query="14-day RSI for NVDA over the past 30 trading days")
```

#### Symbol screening
```
stock_data_query(query="US stocks with market cap > $10B, P/E < 20, and revenue growth > 15% YoY")
```

### Step 5 — Always cite as-of date

Every fetched number ships with the **as-of date** Shibui returns. Include it in your output to the user — never present stale data without timestamp context.

---

## Output Conventions

When you present market data to the user:

- Quote: `**AAPL** $192.45 +1.2% · Vol 48.2M · as of 2026-04-25 close`
- Indicator: `**TSLA RSI(14)** 68.4 (mildly overbought) · as of 2026-04-25`
- Fundamentals: bullet the headline numbers + 1-line interpretation, never dump raw tables

For OHLCV history with many rows, save to `workspace/<task>/<symbol>-history.csv` rather than rendering inline.

---

## What NOT to Do

- Do not present data without an as-of date — stale prices mislead
- Do not extrapolate from one query to another asset class (no futures, FX, crypto via this MCP)
- Do not exceed reasonable query depth — Shibui is free, but courtesy says don't run 100 SQL queries when 5 will do
- Do not fabricate numbers when the MCP can't answer — say "not available via this skill, falling back to web-research"

---

## Fallback (if Shibui MCP not available)

If the user can't / won't install the MCP, downgrade to `web-research`:
- Quotes: search "AAPL stock price now"
- History: search "AAPL daily chart 90 days"
- Fundamentals: search "AAPL 10-Q latest" or company IR page

Always tell the user "I'm using web search instead of structured market data — accuracy and timeliness will be lower."

---

## Asset Class Coverage (platform roadmap)

| Asset class | v1 (this skill) | v2 plan |
|---|---|---|
| US equities | Yes (Shibui) | — |
| US ETFs | Partial (Shibui) | improve |
| Futures (CME) | No — use web-research | self-built yfinance MCP |
| Forex | No — use web-research | self-built MCP |
| Crypto | No — use web-research | dedicated crypto MCP |
| International stocks | No — use web-research | TBD |
""",
            },
        ],
    },
    # ─── Financial Calendar (trading agents) ──────────────
    {
        "name": "Financial Calendar",
        "description": "Look up earnings dates, FOMC meetings, CPI/NFP/GDP release dates, and other macro events that move markets. v1 uses structured web search; v2 will add dedicated MCP.",
        "category": "trading",
        "icon": "FC",
        "folder_name": "financial-calendar",
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Financial Calendar
description: Earnings calendar + macro events (FOMC, CPI, NFP, central banks) via structured web research
---

# Financial Calendar

## When to Use This Skill

Use when a trading agent needs:
- Upcoming earnings release dates for specific companies (or this week's reporters)
- Federal Reserve FOMC meeting dates and minutes release
- US economic data release schedule: CPI, PPI, NFP, GDP, retail sales, ISM, PCE
- Central bank decision dates (ECB, BoE, BoJ, PBoC)
- Geopolitical / fiscal events (debt ceiling, election dates, OPEC meetings)

---

## Implementation Note (v1)

The platform does **not** ship a dedicated calendar MCP server in v1. Smithery doesn't yet have a robust earnings/macro calendar tool. So this skill is a **structured wrapper around `web-research`** with curated query templates and source preferences. v2 will add a dedicated MCP backed by a free API (likely finnhub or trading-economics).

This means: every calendar query in v1 takes a web round-trip. Cache results in `memory/calendar_<month>.md` so the agent doesn't re-fetch the same Fed schedule three times in one week.

---

## Step-by-Step Protocol

### Step 1 — Check memory first

Before web searching, check `memory/calendar_<YYYY-MM>.md` for the current month. If you've already cached this month's events, use them and only web-search for what's missing.

### Step 2 — Run targeted query (use templates below)

#### Earnings calendar
```
web_research("AAPL next earnings date 2026 site:investor.apple.com OR site:nasdaq.com")
```

For a sector / market scan: `"this week earnings calendar US large cap"` then verify each name against IR sources.

#### FOMC schedule
```
web_research("Federal Reserve FOMC meeting schedule 2026 site:federalreserve.gov")
```

Authoritative source: federalreserve.gov/monetarypolicy/fomccalendars.htm — the calendar page directly.

#### US economic data calendar
```
web_research("BLS CPI release schedule 2026 site:bls.gov")
web_research("Bureau of Economic Analysis GDP release schedule 2026 site:bea.gov")
web_research("BLS Employment Situation NFP schedule 2026 site:bls.gov")
```

#### Central bank decisions
```
web_research("ECB Governing Council meeting schedule 2026 site:ecb.europa.eu")
web_research("Bank of England MPC schedule 2026 site:bankofengland.co.uk")
```

#### Aggregate calendar (lower fidelity, faster)
```
web_research("economic calendar this week high impact events")
```
Trusted aggregators: investing.com/economic-calendar, forexfactory.com/calendar, tradingeconomics.com/calendar

### Step 3 — Persist to memory

After each successful fetch, append to `memory/calendar_<YYYY-MM>.md`:

```markdown
## 2026-04 Calendar (last updated: 2026-04-27)

### FOMC
- 2026-04-30: rate decision + press conference (1 day, both PM EDT)
- 2026-06-12: rate decision

### US Data
- 2026-04-30: GDP advance Q1 (8:30am ET, BEA)
- 2026-05-02: NFP April (8:30am ET, BLS)
- 2026-05-13: CPI April (8:30am ET, BLS)

### Earnings (tracked tickers only)
- 2026-04-30 AMC: AAPL Q2 (consensus EPS $1.57)
- 2026-05-01 BMO: AMZN Q1 (consensus EPS $0.99)
```

### Step 4 — Cite source + confidence

Every event ships with:
- The source URL (preferring official: federalreserve.gov, bls.gov, bea.gov)
- A "confidence" tag: `[official]` for sources directly from the agency, `[aggregator]` for investing.com / forexfactory etc.

---

## Output Conventions

For a single event lookup:
```
**AAPL Q2 earnings** — 2026-04-30 AMC (after market close) · consensus EPS $1.57 [aggregator: nasdaq.com]
```

For a weekly briefing block:
```
**This week (2026-04-28 to 2026-05-02)**
- Tue 4/29 — JOLTS (10am, low impact)
- Wed 4/30 — **FOMC decision + presser** (2pm/2:30pm, very high impact)
- Wed 4/30 — GDP Q1 advance (8:30am, high impact)
- Wed 4/30 AMC — **AAPL Q2** (very high impact)
- Fri 5/2 — **NFP April** (8:30am, very high impact)
```

---

## What NOT to Do

- Do not invent dates when web-research returns ambiguous results — say "I couldn't pin down the exact date, here's the source page to check"
- Do not present aggregator data (investing.com etc.) as authoritative when the user is making a decision — escalate to the official agency source
- Do not over-cache — events get rescheduled. Re-verify FOMC and NFP dates within 7 days of the event
- Do not flag everything as "high impact" — distinguish **very high** (FOMC, NFP, CPI), **high** (GDP, retail sales, ISM, mega-cap earnings), **medium** (sector earnings, Fed speakers), **low** (weekly claims, regional Fed indices)

---

## v2 Roadmap

When the platform adds a dedicated finance-calendar MCP server, this skill will switch to direct API calls:

```
get_earnings_calendar(start="2026-04-28", end="2026-05-02")
get_macro_calendar(start="2026-04-28", end="2026-05-02", min_impact="high")
get_econ_event_consensus(event_id="us-cpi-2026-05")
```

Until then, structured web search is the contract.
""",
            },
        ],
    },
    {
        "name": "Full-Stack App Deploy (Vercel + Neon)",
        "description": "Guides the agent through the planning, development, and deployment of a full-stack application (frontend, API routes, database) to Vercel and Neon. Recommend reading this skill at the project's inception to configure tokens, choose frameworks, and design the database architecture upfront, avoiding late-stage deployment surprises.",
        "category": "deploy",
        "icon": "🚀",
        "folder_name": "vercel-full-stack-deploy",
        "is_default": True,
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Full-Stack App Deploy (Vercel + Neon)
description: Guides the agent through the planning, development, and deployment of a full-stack application to Vercel and Neon, ensuring configuration, credentials, and architecture decisions are addressed early.
---

# Full-Stack App Deploy (Vercel + Neon)

## When to Use
Use this skill when the user requests a "website", "web app", or "online system" (product) that requires a database.
If the user only requests static frontend pages without a database or backend APIs, use the existing `publish_page` tool directly.

> [!IMPORTANT]
> **Code Development and Editing Priority:**
> Code development and editing MUST be prioritized inside the local workspace (`workspace`). First develop and edit your changes in the workspace. If the `execute_code` tool is enabled, you can run `npm run build` inside the workspace using bash to verify compilation locally. Otherwise, directly call the `vercel_deploy` tool to deploy the workspace to Vercel (using the default Direct Upload method); if the build fails, use the `vercel_get_deploy_logs` tool to retrieve build logs and fix any errors. Do not write code in remote environments or rely on external triggers.

---

## Step 0: Guide the User to Enable Tools and Configure Tokens

> 🔔 All Vercel/Neon deployment-related tools are disabled by default and must be enabled manually by the user.

**The Agent should proactively check and guide the user through the following actions:**

### 0.1 Check if Vercel Tools are Enabled
- Verify if the Vercel tools under the "deploy" category in the tool list are enabled.
- If not enabled, inform the user:
  "To develop and deploy full-stack applications, you need to enable the Vercel-related tools in the 'Tool Management' page under the 'Deploy' category: Deploy to Vercel, List Vercel Deployments, Get Deploy Logs, Set Environment Variable, and Create Postgres Database. You can also enable Manage Domain if you want to use custom domains."

### 0.2 Guide the User to Sign Up for Vercel and Get a Token
- If Vercel tools are enabled but the `vercel_token` is missing or empty, guide the user:
  1. Visit https://vercel.com/signup to register (supports GitHub / Email sign up).
  2. Once logged in, go to https://vercel.com/account/tokens.
  3. Click "Create" to generate a new token (suggested name: "platform", Scope: "Full Account").
  4. Copy the generated token, return to the platform tool settings page, and paste it into the "Vercel Access Token" configuration field for "Deploy to Vercel" or any other Vercel tools.

### 0.3 Guide the User to Sign Up for Neon and Get an API Key
- If the project requires a database (Postgres), guide the user:
  1. Visit https://neon.tech to register (recommending GitHub OAuth for instant registration).
  2. Once registered, go to the API Keys section in the console settings (https://console.neon.tech/app/settings/api-keys).
  3. Click "Create new API Key", name it (e.g., "platform"), and copy the generated key.
  4. Return to the platform tool settings page, find the `Create Postgres Database` tool, and paste the key into the "Neon API Key" configuration field.

---

## Step 1: Choose Framework and Initialize

### 1.1 Confirm Development Framework
Confirm the framework to be used with the user:
- **Proactively Recommend Next.js**: Explain to the user: "Next.js is the official native framework for Vercel, offering the best integration, zero-config serverless deployments, API routes, and seamless database connections."
- **Default Framework**: If the user has no explicit preference, default to using **Next.js** to initialize the project.
- **Other Options**: If the user explicitly asks for a single-page app (SPA) or lighter alternatives, Vite/Astro can be used, but warn them about independent API hosting limitations.

---

## Step 2: Full-Stack Development and Debugging

### 2.1 Initialize Boilerplate
- Initialize the project using Next.js (prefer non-interactive setup: `npx create-next-app@latest ./ --typescript --eslint --tailwind --src-dir --app --import-alias "@/*"` or modify based on project directory).
- Write backend APIs under `src/app/api/`.

### 2.2 Optimized Deployment & Database Association Sequence (Crucial)
To avoid unnecessary deployments, save Vercel build limits, and prevent serving a broken state without database configuration, strictly follow this sequence:
1. **Create the Database first**: Call the `neon_create_database` tool to obtain the `DATABASE_URL`.
   - **Important**: If the tool returns a "Neon free limit reached" warning, notify the user and guide them to delete old projects or supply an existing database connection string.
2. **Configure Vercel Environment Variables**: Call the `vercel_set_env` tool to inject the `DATABASE_URL` into Vercel.
   - Key: `DATABASE_URL`
   - Value: `<The connection string obtained>`
3. **Deploy the application**: Once the environment variables are successfully configured in Vercel, call the `vercel_deploy` tool to deploy.
   - **Note on Deployment Security**: The deploy tool automatically sends a request to disable Vercel's Deployment Protection (SSO/password protection) on project creation and deployment. This is done to enable full-auto debugging, screenshot verification, and crawling of preview URLs by the AI Agent.

### 2.3 Development, Testing, and Debugging
- **Local Verification (Optional)**: If the `execute_code` tool is enabled, run `npm run build` inside the workspace using the `execute_code` tool (with `bash` language) to ensure there are no compilation or TypeScript errors before deploying. Otherwise, skip local verification and deploy directly.
- **Preview Deployment**: Call `vercel_deploy` (specifying `production=False`) to get a unique Preview URL.
- **Automated Verification**: Use the Browser tool to navigate to the Preview URL, take screenshots, and verify the UI rendering and API operations.
- **Build and Log Debugging**: If the build fails, call `vercel_get_deploy_logs` to view compilation or runtime logs to diagnose and fix errors.
- **Production Deployment**: Once testing is successful, call `vercel_deploy` (specifying `production=True`) to publish to production.

---

## Debugging and Limit Status Monitoring
- **Build Failures** → Use `vercel_get_deploy_logs` to check build logs.
- **Runtime Errors** → Use `vercel_get_deploy_logs` to check runtime logs.
- **Limit Monitoring** → Whenever a deployment completes, check the build logs/Vercel status, and proactively display the Vercel bandwidth/build usage percentage and Neon project limit status (e.g. 1/1 projects). If usage exceeds 80%, highlight it in bold to warn the user.
- **Visual Checks** → Use the Browser tool to screenshot and verify layouts.
"""
            }
        ]
    }
]
