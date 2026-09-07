import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

const baseUrl = process.env.TEST_BASE_URL || 'http://localhost:3008';
const artifactDir = process.env.ARTIFACT_DIR || '/artifacts';
const browser = await chromium.launch({ headless: true });

const json = (route, body) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
});

const installPlatformRoutes = async (page) => {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === '/api/auth/me') {
      return json(route, {
        id: 'admin-user',
        username: 'admin',
        name: '平台管理员',
        role: 'platform_admin',
        tenant_id: 'tenant-1',
        is_active: true,
      });
    }
    if (path === '/api/admin/platform-settings') {
      return json(route, {
        password_login_enabled: true,
        account_registration_enabled: true,
        allow_self_create_company: true,
        sso_custom_domain_redirect_enabled: true,
      });
    }
    if (path === '/api/admin/metrics/timeseries') return json(route, []);
    if (path === '/api/admin/metrics/leaderboards') {
      return json(route, { top_companies: [], top_agents: [] });
    }
    if (path === '/api/admin/metrics/enhanced') {
      return json(route, {
        avg_tokens_per_session_30d: 0,
        retention_rate_7d: 0,
        retained_companies: 0,
        last_week_active_companies: 0,
        channel_distribution: [],
        tool_category_top10: [],
        churn_warnings: [],
      });
    }
    if (path === '/api/enterprise/system-settings/notification_bar') {
      return json(route, { value: { enabled: false, text: '' } });
    }
    if (path === '/api/enterprise/system-settings/platform') {
      return json(route, { value: { public_base_url: 'https://example.test' } });
    }
    if (path === '/api/enterprise/system-settings/system_email_platform') {
      return json(route, { value: {} });
    }
    if (path === '/api/enterprise/email-templates') {
      return json(route, { templates: {}, variables: {}, defaults: {} });
    }
    if (path === '/api/enterprise/identity-providers') return json(route, []);
    if (path.startsWith('/api/tenants/')) {
      return json(route, { id: 'tenant-1', name: '测试企业', slug: 'test' });
    }
    if (path.includes('/notifications/')) return json(route, { unread_count: 0, items: [] });
    return json(route, []);
  });
};

const openPlatformSettings = async (language) => {
  const page = await browser.newPage({
    locale: language === 'zh' ? 'zh-CN' : 'en-US',
    viewport: { width: 1440, height: 1000 },
  });
  await installPlatformRoutes(page);
  await page.addInitScript(({ language }) => {
    localStorage.setItem('token', 'playwright-local-token');
    localStorage.setItem('current_tenant_id', 'tenant-1');
    localStorage.setItem('i18nextLng', language);
  }, { language });
  await page.goto(`${baseUrl}/admin/platform-settings`, { waitUntil: 'networkidle' });
  return page;
};

try {
  const login = await browser.newPage({
    locale: 'zh-CN',
    viewport: { width: 390, height: 844 },
  });
  await login.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/registration-config') {
      return json(route, {
        invitation_code_required: false,
        password_login_enabled: false,
        account_registration_enabled: false,
      });
    }
    if (path === '/api/tenants/resolve-by-domain') {
      return json(route, { id: 'tenant-1', name: '测试企业', sso_enabled: true });
    }
    if (path === '/api/sso/providers') {
      return json(route, [
        { provider_type: 'dingtalk', name: 'DingTalk' },
        { provider_type: 'oauth2', name: 'SSO登录' },
      ]);
    }
    if (path === '/api/auth/providers') return json(route, []);
    return json(route, {});
  });
  await login.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem('i18nextLng', 'zh');
  });
  await login.goto(`${baseUrl}/login`, { waitUntil: 'networkidle' });
  await login.getByRole('button', { name: '钉钉' }).waitFor();
  await login.getByRole('button', { name: 'SSO登录' }).waitFor();
  assert.equal(await login.locator('input[type="password"]').count(), 0,
    'disabled password login must remove its input');
  assert.equal(await login.locator('form button[type="submit"]').count(), 0,
    'disabled password login must remove its submit action');
  const zhCopy = JSON.parse(readFileSync(new URL('../src/i18n/zh.json', import.meta.url), 'utf8'));
  assert.equal(await login.getByRole('link', { name: zhCopy.auth.goRegister, exact: true }).count(), 0,
    'disabled registration must remove its entry');
  assert.equal(await login.locator('a[href="/forgot-password"]').count(), 0);
  assert.equal(await login.getByRole('button', { name: 'SSO登录' }).isEnabled(), true,
    'SSO must remain available when password login is disabled');
  await login.screenshot({ path: `${artifactDir}/login-mobile-clean.png`, fullPage: true });
  await login.close();

  const zh = await openPlatformSettings('zh');
  const zhDashboardText = await zh.locator('body').innerText();
  for (const expected of ['公司数量', '渠道分布', '无流失预警 — 所有活跃公司状态健康']) {
    if (!zhDashboardText.includes(expected)) {
      throw new Error(`Chinese platform dashboard is missing: ${expected}`);
    }
  }
  if (zhDashboardText.includes('Channel Distribution')) {
    throw new Error('dashboard contains untranslated English copy');
  }
  await zh.screenshot({ path: `${artifactDir}/platform-settings-dashboard-zh.png`, fullPage: true });
  await zh.locator('.tab').filter({ hasText: '平台' }).click();
  const zhConfigText = await zh.locator('body').innerText();
  for (const expected of ['OAuth 登录', '系统邮件配置', '邮件模板', '启用账号密码登录']) {
    if (!zhConfigText.includes(expected)) {
      throw new Error(`Chinese platform configuration is missing: ${expected}`);
    }
  }
  if (zhConfigText.includes('Enable password login')) {
    throw new Error('platform configuration contains untranslated English copy');
  }
  await zh.screenshot({ path: `${artifactDir}/platform-settings-config-zh.png`, fullPage: true });
  await zh.close();

  const en = await openPlatformSettings('en');
  const enDashboardText = await en.locator('body').innerText();
  for (const expected of ['Companies', 'Channel Distribution', 'No churn warnings — all active companies are healthy']) {
    if (!enDashboardText.includes(expected)) {
      throw new Error(`English platform dashboard is missing: ${expected}`);
    }
  }
  await en.locator('.tab').filter({ hasText: 'Platform' }).click();
  const enConfigText = await en.locator('body').innerText();
  for (const expected of ['OAuth login', 'System Email Configuration']) {
    if (!enConfigText.includes(expected)) {
      throw new Error(`English platform configuration is missing: ${expected}`);
    }
  }
  await en.screenshot({ path: `${artifactDir}/platform-settings-config-en.png`, fullPage: true });
  await en.close();

  console.log('login cleanup and platform settings i18n Playwright validation passed');
} finally {
  await browser.close();
}
