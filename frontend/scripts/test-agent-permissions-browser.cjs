// Run inside the Playwright Docker image against an isolated nginx/API/PG stack.
const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

async function main() {
    const auth = JSON.parse(fs.readFileSync(process.env.AUTH_FIXTURE));
    const base = process.env.BASE_URL;
    const output = process.env.OUTPUT_DIR;
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(value => {
        localStorage.setItem('token', value.token);
        localStorage.setItem('current_tenant_id', value.tenant);
        localStorage.setItem('i18nextLng', 'zh');
    }, auth);
    const permissions = async agent => {
        const response = await page.request.get(`${base}/api/agents/${agent}/permissions`, {
            headers: { Authorization: `Bearer ${auth.token}` },
        });
        assert.equal(response.status(), 200);
        return response.json();
    };
    const saving = () => page.waitForResponse(response =>
        response.url().endsWith('/permissions') && response.request().method() === 'PUT');
    try {
        const original = (await permissions(auth.agent)).grants.filter(grant => grant.scope_type !== 'company');
        const assertPreserved = state => {
            for (const expected of original) {
                assert(state.grants.some(grant =>
                    grant.scope_type === expected.scope_type && grant.scope_id === expected.scope_id
                    && grant.access_level === expected.access_level), JSON.stringify(expected));
            }
        };
        await page.goto(`${base}/agents/${auth.agent}/settings#settings`);
        for (const [label, level] of [
            ['可使用', 'use'], ['不开放', null],
            ['可管理', 'manage'], ['可使用', 'use'],
        ]) {
            const radio = page.getByRole('radio', { name: label, exact: true });
            if (!await radio.isChecked()) {
                const [response] = await Promise.all([saving(), radio.click()]);
                assert.equal(response.status(), 200);
                await page.locator('.agent-permissions-editor[aria-busy="false"]').waitFor();
            }
            assert(await radio.isChecked());
            const state = await permissions(auth.agent);
            assertPreserved(state);
            assert(state.grants.some(grant => grant.scope_id === auth.manager && grant.access_level === 'manage'));
            assert.equal(state.company_access_level, level);
        }
        // A rejected save must leave the last persisted company choice selected.
        await page.route('**/permissions', async route => {
            if (route.request().method() === 'PUT') {
                await route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } });
            } else await route.continue();
        });
        await page.getByRole('radio', { name: '可管理', exact: true }).click();
        await page.getByRole('status').filter({ hasText: '权限保存失败' }).waitFor();
        assert(await page.getByRole('radio', { name: '可使用', exact: true }).isChecked());
        assert.equal((await permissions(auth.agent)).company_access_level, 'use');
        await page.unroute('**/permissions');
        await page.getByRole('button', { name: '选择部门或成员', exact: true }).click();
        const dialog = page.getByRole('dialog');
        await dialog.getByRole('button', { name: /^Operations/ }).click();
        await dialog.locator('.org-access-picker__department-grant input').check();
        await dialog.locator('.org-access-picker__selected-row--department').filter({ hasText: 'Operations' })
            .locator('select').selectOption('manage');
        const saved = saving();
        await dialog.getByRole('button', { name: '保存设置', exact: true }).click();
        assert.equal((await saved).status(), 200);
        await dialog.waitFor({ state: 'hidden' });
        const state = await permissions(auth.agent);
        assertPreserved(state);
        assert(state.grants.some(grant => grant.scope_type === 'department' && grant.access_level === 'manage'));
        assert.equal(state.company_access_level, 'use');
        await page.screenshot({ path: `${output}/permissions-complete.png`, fullPage: true });
        const panel = page.locator('.agent-permissions-editor');
        await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
        await panel.screenshot({ path: `${output}/permissions-light.png`, animations: 'disabled' });
        await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
        await panel.screenshot({ path: `${output}/permissions-dark.png`, animations: 'disabled' });
        await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));

        // The creation page uses the same picker without an existing Agent ID.
        await page.goto(`${base}/agents/new?type=openclaw`);
        await page.locator('.form-group input').first().fill('Browser permission assistant');
        await page.getByRole('button', { name: '选择部门或成员', exact: true }).click();
        await dialog.locator('.org-access-picker__member-row').filter({ hasText: 'manager' })
            .getByRole('checkbox').check();
        await dialog.locator('.org-access-picker__selected-row').filter({ hasText: 'manager' })
            .locator('select').selectOption('manage');
        await dialog.getByRole('button', { name: '保存设置', exact: true }).click();
        await dialog.waitFor({ state: 'hidden' });
        // Creator-only is an explicit bulk reset, with a safe cancel path.
        await page.getByRole('button', { name: '清除授权', exact: true }).click();
        await page.getByRole('dialog', { name: '清除授权', exact: true })
            .getByRole('button', { name: '取消', exact: true }).click();
        await page.getByRole('dialog', { name: '清除授权', exact: true }).waitFor({ state: 'hidden' });
        assert(await page.getByRole('radio', { name: '可使用', exact: true }).isChecked());
        assert(await panel.getByText('manager', { exact: true }).isVisible());

        await page.getByRole('button', { name: '清除授权', exact: true }).click();
        await page.getByRole('button', { name: '清除', exact: true }).click();
        await page.locator('.agent-permissions-editor input[value="off"]:checked').waitFor();
        assert(await page.getByRole('radio', { name: '不开放', exact: true }).isChecked());
        assert.equal(await panel.locator('li').count(), 0);
        await page.getByRole('radio', { name: '可使用', exact: true }).click();
        await page.locator('.agent-permissions-editor[aria-busy="false"]').waitFor();
        await page.getByRole('button', { name: '选择部门或成员', exact: true }).click();
        await dialog.locator('.org-access-picker__member-row').filter({ hasText: 'manager' })
            .getByRole('checkbox').check();
        await dialog.locator('.org-access-picker__selected-row').filter({ hasText: 'manager' })
            .locator('select').selectOption('manage');
        assert.equal(await dialog.locator('.org-access-picker__selected-row').count(), 1);
        await dialog.getByRole('button', { name: '保存设置', exact: true }).click();
        await dialog.waitFor({ state: 'hidden' });
        await panel.getByText('manager', { exact: true }).waitFor();
        await page.screenshot({ path: `${output}/permissions-create.png`, fullPage: true });
        const created = page.waitForResponse(response =>
            response.url().endsWith('/api/agents/') && response.request().method() === 'POST');
        await page.getByRole('button', { name: '连接数字员工', exact: true }).click();
        const response = await created;
        assert.equal(response.status(), 201);
        const agent = await response.json();
        const initial = await permissions(agent.id);
        assert.equal(initial.company_access_level, 'use');
        assert(initial.grants.some(grant => grant.scope_id === auth.manager && grant.access_level === 'manage'));
        console.log('PASS: company and picker saves preserve every explicit grant, including admins and inactive departments; creation persists the same grants.');
    } finally {
        await browser.close();
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
