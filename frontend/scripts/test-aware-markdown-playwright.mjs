import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';

// Run in Docker against the local frontend, with short-lived local review auth.
const baseUrl = process.env.TEST_BASE_URL || 'http://host.docker.internal:3008';
const { token, agentId } = JSON.parse(await readFile(process.env.TEST_AUTH_FILE, 'utf8'));
const output = process.env.TEST_OUTPUT_DIR || '/tmp/aware-markdown';
await mkdir(output, { recursive: true });

const longDescription = [
    '**执行步骤**：检查工单回复状态，超过 `24 小时` 未回复时提醒负责人。',
    '先核对最新回复与处理进展，再判断是否需要催办；没有超时工单时不发送消息。',
    [
        '1. 拉取待处理工单，核对负责人、最近回复时间和当前处理状态。',
        '2. 检查回复是否包含明确的处理方案与时间节点，避免重复提醒。',
        '3. 对超过时限且没有有效回复的工单发送提醒，并记录检查结果。',
    ].join('\n'),
    '> 仅向负责人发送提醒，已解决的工单不再催办。',
    '[查看说明](https://example.com/guide)',
    '```text\n保留完整执行说明\n检查结束\n```',
].join('\n\n');
const trigger = (id, reason) => ({
    id, name: id, focus_ref: 'review-focus', type: 'cron',
    config: { expr: '9 6,13,17,21 * * *' }, reason,
    is_enabled: true, is_system: false, fire_count: 145,
    last_fired_at: '2026-09-03T13:09:00Z',
});

const browser = await chromium.launch({ headless: true });
const errors = [];
const page = await browser.newPage({ viewport: { width: 1800, height: 1100 }, locale: 'zh-CN' });
page.on('pageerror', (error) => errors.push(error.message));
await page.addInitScript(({ token }) => {
    localStorage.setItem('token', token);
    localStorage.setItem('i18nextLng', localStorage.getItem('i18nextLng') || 'zh');
    localStorage.setItem('theme', 'dark');
}, { token });

async function openAware(route = 'chat') {
    await page.goto(`${baseUrl}/agents/${agentId}/${route}`);
    await page.getByRole(route === 'settings' ? 'tab' : 'button', { name: '自我意识', exact: true }).first().click();
    await page.locator('.aware-focus-row').first().waitFor();
}

async function widenPanel() {
    const handle = await page.locator('.live-panel-resize-handle').boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + 40);
    await page.mouse.down();
    await page.mouse.move(100, handle.y + 40, { steps: 10 });
    await page.mouse.up();
}

async function assertCompact(card) {
    const metrics = await card.locator('.expandable-markdown__viewport').evaluate((element) => ({
        height: element.getBoundingClientRect().height,
        lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
        fullHeight: element.scrollHeight,
    }));
    assert(metrics.height <= metrics.lineHeight * 3 + 1, JSON.stringify(metrics));
    assert(metrics.fullHeight > metrics.height, `Long description must have additional content: ${JSON.stringify(metrics)}`);
}

try {
    // First exercise live GET APIs and the actual local application, without fixtures.
    await openAware();
    await widenPanel();
    const rows = page.locator('.aware-focus-row');
    let liveCards = 0;
    for (let index = 0; index < await rows.count(); index += 1) {
        await rows.nth(index).click();
        liveCards = await page.locator('.aware-trigger-row').count();
        if (liveCards) break;
    }
    assert(liveCards > 0, 'Local review agent must expose a linked trigger');
    await page.locator('.live-panel').screenshot({ path: `${output}/live-collapsed.png` });
    console.log(`Live local API/UI: ${liveCards} linked trigger(s) rendered`);

    // Browser-only fixtures cover Markdown variants; no test records are written to the DB.
    await page.route(`**/api/agents/${agentId}/focus/**`, (route) => route.fulfill({ json: [{
        id: 'review-focus-id', agent_id: agentId, key: 'review-focus',
        title: '工单回复监控', description: longDescription,
        status: 'in_progress', kind: 'normal', source: 'user',
        created_at: '2026-08-20T02:21:00Z',
    }] }));
    await page.route(`**/api/agents/${agentId}/triggers`, (route) => route.fulfill({ json: [
        trigger('review-long', longDescription),
        trigger('review-short', '**检查工单**，无需催办。'),
    ] }));
    await openAware();
    await widenPanel();
    const focusDescription = page.locator('.aware-focus-description');
    await focusDescription.getByRole('button', { name: '展开完整说明', exact: true }).waitFor();
    await assertCompact(focusDescription);
    assert.equal(await focusDescription.locator('strong').first().innerText(), '执行步骤');
    assert.equal(await focusDescription.locator('code').first().innerText(), '24 小时');
    await focusDescription.getByRole('button', { name: '展开完整说明', exact: true }).click();
    assert.match(await focusDescription.locator('pre').innerText(), /检查结束/);
    await focusDescription.getByRole('button', { name: '收起说明', exact: true }).click();
    await assertCompact(focusDescription);
    const card = page.locator('.aware-trigger-row').first();
    const shortCard = page.locator('.aware-trigger-row').nth(1);
    const expand = card.getByRole('button', { name: '展开完整说明', exact: true });
    await expand.waitFor();
    await assertCompact(card);
    assert.equal(await shortCard.locator('.expandable-markdown__toggle').count(), 0);
    assert.equal(await card.locator('.markdown-renderer strong').first().innerText(), '执行步骤');
    assert.equal(await card.locator('.markdown-renderer code').first().innerText(), '24 小时');
    assert.equal(await card.locator('.markdown-renderer li').count(), 3);
    assert.equal(await card.locator('.markdown-renderer a').getAttribute('href'), 'https://example.com/guide');
    assert.equal(await page.getByRole('tab', { name: /关注点/ }).locator('b').innerText(), '1');
    await expand.focus();
    await page.keyboard.press('Enter');
    const collapse = card.getByRole('button', { name: '收起说明', exact: true });
    assert.equal(await collapse.getAttribute('aria-expanded'), 'true');
    const expandedHeight = await card.locator('.expandable-markdown__viewport').evaluate((element) => element.clientHeight);
    assert(expandedHeight > 100);
    assert.match(await card.locator('pre').innerText(), /检查结束/);
    await page.locator('.live-panel').screenshot({ path: `${output}/expanded.png` });
    await collapse.click();
    await assertCompact(card);
    await card.getByRole('button', { name: '配置', exact: true }).click();
    await page.locator('.aware-config-drawer').waitFor();
    await page.keyboard.press('Escape');
    await page.locator('.aware-config-drawer').waitFor({ state: 'hidden' });
    await page.locator('.live-panel').screenshot({ path: `${output}/collapsed.png` });

    // The existing chat shell hides all side panels at 900px and below.
    for (const width of [1200, 960]) {
        await page.setViewportSize({ width, height: 1000 });
        await card.waitFor({ state: 'visible' });
        await assertCompact(card);
        const overflow = await page.locator('.aware-workspace').evaluate((element) => element.scrollWidth - element.clientWidth);
        assert(overflow <= 1, `Workspace overflows at ${width}px: ${overflow}`);
    }
    await page.locator('.live-panel').screenshot({ path: `${output}/narrow.png` });
    await page.setViewportSize({ width: 1800, height: 1100 });
    await openAware('settings');
    await focusDescription.getByRole('button', { name: '展开完整说明', exact: true }).waitFor();
    await assertCompact(focusDescription);
    await page.locator('.aware-workspace').screenshot({ path: `${output}/focus-detail.png` });
    await page.evaluate(() => localStorage.setItem('i18nextLng', 'en'));
    await page.reload();
    await page.getByRole('tab', { name: 'Aware', exact: true }).first().click();
    await page.getByRole('button', { name: 'Show full description', exact: true }).first().waitFor();
    assert.deepEqual(errors, [], 'Browser must not emit runtime errors');
    console.log('Passed: Markdown semantics, 3-line preview, short content, keyboard expansion, collapse, configuration, count, responsive layouts, English copy');
} finally {
    await browser.close();
}
