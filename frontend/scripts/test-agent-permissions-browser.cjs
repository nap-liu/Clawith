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
        for (const label of ['关闭', '使用']) {
            await page.getByRole('combobox', { name: '公司开放', exact: true }).click();
            const saved = saving();
            await page.getByRole('option', { name: label, exact: true }).click();
            assert.equal((await saved).status(), 200);
            const state = await permissions(auth.agent);
            assertPreserved(state);
            assert(state.grants.some(grant => grant.scope_id === auth.manager && grant.access_level === 'manage'));
            assert.equal(state.company_access_level, label === '关闭' ? null : 'use');
        }
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
