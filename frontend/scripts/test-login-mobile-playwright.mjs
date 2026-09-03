import { chromium } from 'playwright';

const baseUrl = process.env.TEST_BASE_URL || 'http://local-ai.yeyecha.com:3008';
const browser = await chromium.launch({ headless: true });

const viewports = [
  { name: 'phone', width: 390, height: 844 },
  { name: 'large-mobile', width: 884, height: 1650 },
];

try {
  for (const viewport of viewports) {
    const page = await browser.newPage({
      locale: 'zh-CN',
      viewport: { width: viewport.width, height: viewport.height },
    });

    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = {};
      if (path === '/api/auth/registration-config') {
        body = {
          invitation_code_required: false,
          password_login_enabled: true,
          account_registration_enabled: true,
        };
      } else if (path === '/api/auth/providers') {
        body = [];
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    });

    await page.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem('i18nextLng', 'zh');
    });

    await page.goto(`${baseUrl}/login`, { waitUntil: 'networkidle' });
    const emailInput = page.locator('input[type="email"]');
    await emailInput.waitFor({ state: 'visible' });

    const metrics = await page.evaluate(() => {
      const heroElement = document.querySelector('.atlas-login-hero');
      const hero = heroElement?.getBoundingClientRect();
      const heroStyle = heroElement ? getComputedStyle(heroElement) : null;
      const email = document.querySelector('input[type="email"]')?.getBoundingClientRect();
      const emailStyle = document.querySelector('input[type="email"]')
        ? getComputedStyle(document.querySelector('input[type="email"]'))
        : null;
      return {
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        scrollWidth: document.documentElement.scrollWidth,
        heroTop: hero?.top ?? Number.POSITIVE_INFINITY,
        heroBottom: hero?.bottom ?? Number.POSITIVE_INFINITY,
        heroHeight: hero?.height ?? Number.POSITIVE_INFINITY,
        heroMinHeight: heroStyle?.minHeight ?? '',
        heroPadding: heroStyle
          ? `${heroStyle.paddingTop} ${heroStyle.paddingBottom}`
          : '',
        emailBottom: email?.bottom ?? Number.POSITIVE_INFINITY,
        emailFontSize: emailStyle ? Number.parseFloat(emailStyle.fontSize) : 0,
      };
    });

    if (metrics.scrollWidth > metrics.viewportWidth + 1) {
      throw new Error(`${viewport.name}: horizontal overflow ${metrics.scrollWidth}px`);
    }
    if (metrics.heroBottom > Math.min(600, metrics.viewportHeight * 0.55)) {
      throw new Error(`${viewport.name}: hero remains too tall (${metrics.heroBottom}px)`);
    }
    if (metrics.emailBottom > metrics.viewportHeight) {
      throw new Error(`${viewport.name}: email input is below the first viewport (${metrics.emailBottom}px)`);
    }
    if (metrics.emailFontSize < 16) {
      throw new Error(`${viewport.name}: input font can trigger mobile browser zoom`);
    }

    await page.close();
  }

  console.log('mobile login Playwright validation passed');
} finally {
  await browser.close();
}
