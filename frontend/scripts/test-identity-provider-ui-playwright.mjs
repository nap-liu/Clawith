import { chromium } from 'playwright';

const baseUrl = process.env.TEST_BASE_URL || 'http://local-ai.yeyecha.com:3008';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ locale: 'zh-CN' });
let loginDiscoveryAttempts = 0;
let observedDirectoryTarget = '';
const departmentParentRequests = [];
let conflictResolved = false;
let mergeRequest = null;
let avatarRequests = 0;

await page.route('https://avatars.example.test/**', async (route) => {
  avatarRequests += 1;
  await route.fulfill({
    status: 200,
    contentType: 'image/png',
    body: Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
      'base64',
    ),
  });
});

const provider = {
  id: '10000000-0000-0000-0000-000000000001',
  provider_type: 'oauth2',
  name: 'Internal SSO / SCIM',
  is_active: true,
  sso_login_enabled: true,
  sync_enabled: false,
  config: {
    app_id: 'client',
    authorize_url: 'https://login.example/oauth2/authorize',
    token_url: 'https://login.example/oauth2/token',
    user_info_url: 'https://login.example/oauth2/userinfo',
    scim_base_url: 'https://login.example/scim/v2',
    directory_protocol: 'scim',
    field_mapping: {},
    directory: { field_mapping: {} },
  },
};

await page.route('**/api/**', async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  const json = (body) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
  if (path === '/api/auth/me') {
    return json({
      id: '20000000-0000-0000-0000-000000000001',
      username: 'admin',
      name: '管理员',
      role: 'org_admin',
      tenant_id: '30000000-0000-0000-0000-000000000001',
      is_active: true,
    });
  }
  if (path.startsWith('/api/tenants/')) {
    return json({
      id: '30000000-0000-0000-0000-000000000001',
      name: '测试租户',
      slug: 'test',
    });
  }
  if (path === '/api/enterprise/identity-providers') {
    return json([
      provider,
      { id: 'configured-dingtalk', provider_type: 'dingtalk', name: 'DingTalk', is_active: true, config: {} },
      { id: 'configured-wecom', provider_type: 'wecom', name: 'Wecom', is_active: true, config: {} },
      { id: 'legacy-wechat', provider_type: 'wechat', name: 'Wechat', config: {} },
      { id: 'legacy-platform', provider_type: 'platform', name: 'Platform', config: {} },
    ]);
  }
  if (path.endsWith('/discover-field-paths')) {
    if (url.searchParams.get('capability') === 'directory') {
      observedDirectoryTarget = url.searchParams.get('target_account') || '';
    }
    if (url.searchParams.get('capability') === 'login' && loginDiscoveryAttempts++ === 0) {
      return json({ source: 'authorization_required', fields: [], paths: [] });
    }
    return json({
      fields: url.searchParams.get('capability') === 'login'
        ? [
            { path: 'sub', sample_value: 'user-42' },
            { path: 'data.employee_code', sample_value: 'E-0042' },
            { path: 'data.avatar.original', sample_value: 'https://cdn.example/avatar.png' },
          ]
        : [
            { path: '/displayName', sample_value: '测试成员' },
            {
              path: '/emails',
              sample_value: '[{"primary":true,"value":"member@example.com","type":"work"}]',
            },
            { path: '/profile/jobTitle', sample_value: '前端工程师' },
            { path: '/photos', sample_value: '[{"primary":true,"value":"https://cdn.example/avatar.png"}]' },
          ],
    });
  }
  if (path === '/api/enterprise/org/sync-runs') return json([{
    id: 'sync-run-1',
    provider_id: provider.id,
    status: 'needs_review',
    stats: { identity_conflicts: 1 },
    created_at: '2026-09-03T12:00:00Z',
  }]);
  if (path.endsWith('/identity-conflicts/conflict-1/resolve')) {
    mergeRequest = request.postDataJSON();
    conflictResolved = true;
    return json({});
  }
  if (path === '/api/enterprise/identity-conflicts') return json({
    total: 1,
    items: [{
      id: 'conflict-1',
      provider_name: 'Internal SSO / SCIM',
      source: 'directory_contact',
      masked_identifier: '••••A1B2C3D4',
      reason: 'lower_priority_identity_mismatch',
      matched_by: 'phone',
      conflicting_fields: ['email'],
      recommended_action: 'correct_source_and_resync',
      status: conflictResolved ? 'resolved' : 'pending',
      outcome: null,
      resolution_action: conflictResolved ? 'merge_users' : null,
      bound_user: {
        reference: 'USR-CURRENT',
        display_name: '当前绑定用户',
        avatar_url: null,
        title: '产品经理',
        phone: '13800000000',
        email: 'current@example.com',
        is_active: true,
      },
      merge_candidates: [{
        reference: 'USR-CURRENT',
        display_name: '当前绑定用户',
        avatar_url: null,
        title: '产品经理',
        phone: '13800000000',
        email: 'current@example.com',
        is_active: true,
      }, {
        reference: 'USR-TARGET',
        display_name: '目标平台用户',
        avatar_url: null,
        title: '研发工程师',
        phone: '13900000000',
        email: 'target@example.com',
        is_active: true,
      }],
      evidence: [{
        field: 'phone',
        source_value: '13900000000',
        candidate_user: {
          reference: 'USR-TARGET',
          display_name: '目标平台用户',
          avatar_url: null,
          title: '研发工程师',
          phone: '13900000000',
          email: 'target@example.com',
          is_active: true,
        },
        is_highest_priority: true,
        is_conflicting: false,
      }, {
        field: 'email',
        source_value: 'source@example.com',
        candidate_user: null,
        is_highest_priority: false,
        is_conflicting: true,
      }],
      allowed_actions: conflictResolved ? [] : ['rebind_source_to_highest_priority', 'merge_users', 'keep_people_separate'],
      repair_unavailable_reason: null,
      evidence_changed: false,
      created_at: '2026-09-03T12:00:00Z',
    }],
  });
  if (path === '/api/enterprise/org/departments') {
    const parentId = url.searchParams.get('parent_id');
    departmentParentRequests.push(parentId);
    return json({
      items: parentId === 'company-root'
        ? [{ id: 'dept-1', name: '技术部', parent_id: 'company-root', member_count: 1, has_children: false }]
        : [{ id: 'company-root', name: '测试租户', member_count: 1, has_children: true }],
      total_member: parentId ? 0 : 1,
    });
  }
  if (path === '/api/enterprise/org/members') {
    return json([{
      id: 'member-1',
      name: '测试成员',
      nickname: '成员昵称',
      title: '工程师',
      phone_masked: '138****0042',
      department_path: '测试租户/技术部',
      avatar_url: 'https://avatars.example.test/provider-member.png',
      provider_type: 'oauth2',
      directory_protocol: 'scim',
    }]);
  }
  if (path === '/api/enterprise/stats') {
    return json({ total_users: 1, running_agents: 0, total_agents: 0, pending_approvals: 0 });
  }
  if (path === '/api/tenants') return json([]);
  if (path.includes('/notifications/')) return json({ unread_count: 0, items: [] });
  return json([]);
});

await page.addInitScript(() => {
  localStorage.setItem('token', 'playwright-local-token');
  localStorage.setItem('current_tenant_id', '30000000-0000-0000-0000-000000000001');
  localStorage.setItem('i18nextLng', 'zh');
});

try {
  await page.goto(`${baseUrl}/enterprise#org`, { waitUntil: 'networkidle' });
  await page.getByText('组织架构同步', { exact: true }).waitFor();
  if (await page.getByText('Wechat', { exact: true }).count()) throw new Error('legacy Wechat provider is visible');
  if (await page.getByText('Platform', { exact: true }).count()) throw new Error('legacy Platform provider is visible');
  await page.getByText('钉钉', { exact: true }).waitFor();
  await page.getByText('企业微信', { exact: true }).waitFor();
  if (await page.getByText('DingTalk', { exact: true }).count()) throw new Error('configured DingTalk name bypassed zh i18n');
  if (await page.getByText('Wecom', { exact: true }).count()) throw new Error('configured Wecom name bypassed zh i18n');

  await page.getByText('Internal SSO / SCIM', { exact: true }).click();
  await page.getByText('目录字段映射', { exact: true }).waitFor();

  const rootMapping = page.getByLabel('企业根组织');
  if (await rootMapping.inputValue() !== '测试租户') {
    throw new Error('enterprise root mapping did not default to the tenant name');
  }

  const discoverButtons = page.getByRole('button', { name: '探测字段' });
  await discoverButtons.nth(0).click();
  await page.waitForTimeout(250);
  const userIdField = page.getByLabel('登录字段映射 - 用户 ID 字段');
  await userIdField.fill('data.userId');
  if (await userIdField.inputValue() !== 'data.userId') {
    throw new Error('manual OAuth path was not preserved exactly');
  }

  await discoverButtons.nth(0).click();
  await page.waitForTimeout(250);
  await page.locator('datalist option[value="data.employee_code"]').first().waitFor({ state: 'attached' });
  const loginListId = await userIdField.getAttribute('list');
  const loginOption = page.locator(`datalist[id="${loginListId}"] option[value="data.employee_code"]`);
  if (await loginOption.getAttribute('label') !== 'E-0042 (data.employee_code)') {
    throw new Error('OAuth discovered field label is incomplete');
  }
  const loginAvatarField = page.getByLabel('登录字段映射 - 头像字段');
  const loginAvatarListId = await loginAvatarField.getAttribute('list');
  const loginAvatarOption = page.locator(`datalist[id="${loginAvatarListId}"] option[value="data.avatar.original"]`);
  if (await loginAvatarOption.getAttribute('label') !== 'https://cdn.example/avatar.png (data.avatar.original)') {
    throw new Error('OAuth avatar field mapping is unavailable');
  }

  const directoryTarget = page.getByLabel('目标账号');
  await directoryTarget.fill('member@example.com');
  await discoverButtons.nth(1).click();
  await page.locator('datalist option[value="/profile/jobTitle"]').first().waitFor({ state: 'attached' });
  if (observedDirectoryTarget !== 'member@example.com') {
    throw new Error('SCIM field discovery did not send the target account');
  }
  const titleField = page.getByLabel('目录字段映射 - 职位');
  const directoryListId = await titleField.getAttribute('list');
  const titleOption = page.locator(`datalist[id="${directoryListId}"] option[value="/profile/jobTitle"]`);
  if (await titleOption.getAttribute('label') !== '前端工程师 (/profile/jobTitle)') {
    throw new Error('SCIM discovered field label is incomplete');
  }

  const emailField = page.getByLabel('目录字段映射 - 邮箱');
  const emailListId = await emailField.getAttribute('list');
  const emailOption = page.locator(`datalist[id="${emailListId}"] option[value="/emails"]`);
  if (await emailOption.getAttribute('label') !== 'member@example.com (/emails)') {
    throw new Error('raw SCIM array JSON was not reduced to its primary value');
  }
  const directoryAvatarField = page.getByLabel('目录字段映射 - 头像');
  const directoryAvatarListId = await directoryAvatarField.getAttribute('list');
  const directoryAvatarOption = page.locator(`datalist[id="${directoryAvatarListId}"] option[value="/photos"]`);
  if (await directoryAvatarOption.getAttribute('label') !== 'https://cdn.example/avatar.png (/photos)') {
    throw new Error('SCIM avatar field mapping is unavailable');
  }

  await page.getByRole('button', { name: '查看身份冲突' }).click();
  await page.getByRole('heading', { name: '需要人工检查的身份冲突' }).waitFor();
  await page.getByText('13900000000', { exact: true }).first().waitFor();
  await page.getByText('目标平台用户', { exact: true }).first().waitFor();
  await page.getByRole('button', { name: '仅重绑当前来源' }).waitFor();
  await page.getByRole('button', { name: '确认两人保持分离' }).waitFor();
  await page.getByRole('combobox', { name: '主用户' }).click();
  await page.getByRole('option', { name: /当前绑定用户/ }).click();
  await page.getByRole('combobox', { name: '保留手机号' }).click();
  await page.getByRole('option', { name: '13900000000 · 目标平台用户' }).waitFor();
  await page.getByRole('option', { name: '13800000000 · 当前绑定用户' }).waitFor();
  await page.getByRole('option', { name: '13900000000 · 目标平台用户' }).click();
  await page.getByRole('combobox', { name: '保留邮箱' }).click();
  await page.getByRole('option', { name: 'source@example.com · 来源账号' }).click();
  if (process.env.SCREENSHOT_PATH) {
    await page.locator('.identity-conflict-modal').screenshot({ path: process.env.SCREENSHOT_PATH });
  }
  await page.getByRole('button', { name: '合并并保留 当前绑定用户' }).click();
  await page.getByRole('button', { name: '确认执行' }).click();
  await page.getByText('已解决：合并用户', { exact: true }).waitFor();
  if (
    mergeRequest?.action !== 'merge_users'
    || mergeRequest?.target_reference !== 'USR-CURRENT'
    || mergeRequest?.field_sources?.phone !== 'USR-TARGET'
    || mergeRequest?.field_sources?.email !== 'SOURCE_ACCOUNT'
  ) {
    throw new Error('identity conflict merge did not submit the selected retained user');
  }
  await page.getByRole('button', { name: '关闭' }).click();

  const rootButton = page.getByRole('button', { name: '测试租户' });
  if (await page.getByRole('button', { name: /技术部/ }).count()) {
    throw new Error('department tree was expanded before user interaction');
  }
  await rootButton.click();
  await page.getByRole('button', { name: /技术部/ }).waitFor();
  if (!departmentParentRequests.includes('company-root')) {
    throw new Error('department tree did not lazy-load the selected parent');
  }

  const memberName = page.getByText('测试成员（成员昵称） · 138****0042', { exact: true });
  const memberCard = memberName.locator('..');
  const memberText = await memberCard.innerText();
  if (memberText.includes('Nickname') || memberText.includes('昵称:')) {
    throw new Error('nickname was rendered as a separate metadata row');
  }
  if (memberText.includes('SCIM 2.0')) throw new Error('provider type prefix is visible');
  if (
    !memberText.includes('工程师')
    || !memberText.includes('138****0042')
    || !memberText.includes('测试租户/技术部')
  ) {
    throw new Error('member metadata is incomplete');
  }
  const memberAvatar = memberName.locator('xpath=../..').locator('.ui-avatar > img');
  await memberAvatar.waitFor({ state: 'visible' });
  if (await memberAvatar.getAttribute('src') !== 'https://avatars.example.test/provider-member.png') {
    throw new Error('provider member avatar did not preserve its API image URL');
  }
  const imageMetrics = await memberAvatar.evaluate((image) => ({
    complete: image.complete,
    naturalWidth: image.naturalWidth,
  }));
  if (!imageMetrics.complete || imageMetrics.naturalWidth < 1 || avatarRequests < 1) {
    throw new Error('provider member avatar image was not fetched and rendered');
  }
  if (process.env.SCREENSHOT_PATH) {
    await page.screenshot({ path: process.env.SCREENSHOT_PATH, fullPage: true });
  }
  console.log('identity provider Playwright validation passed');
} finally {
  await browser.close();
}
