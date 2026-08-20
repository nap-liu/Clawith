import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const readSource = (path) => readFileSync(resolve(__dirname, path), 'utf8');

const dialogSource = readSource('../src/components/Dialog/DialogProvider.tsx');
const dialogStyles = readSource('../src/components/Dialog/DialogProvider.css');
const skillMarketSource = readSource('../src/pages/SkillMarket.tsx');
const cliWizardSource = readSource('../src/components/cli-tools/CliToolWizard.tsx');
const cliSectionSource = readSource('../src/components/cli-tools/CliToolsSection.tsx');
const confirmSource = readSource('../src/components/ConfirmModal.tsx');
const promptSource = readSource('../src/components/PromptModal.tsx');

assert.match(dialogSource, /export function Modal\(/);
assert.match(dialogSource, /export function Drawer\(/);
assert.match(dialogSource, /useOverlayPresence\(open, onAfterClose\)/);
assert.doesNotMatch(dialogSource, /app-modal-overlay[^>]*onClick=/);
assert.doesNotMatch(dialogSource, /app-drawer-overlay[^>]*on(?:Click|MouseDown)=/);

assert.match(dialogStyles, /data-state="open"/);
assert.match(dialogStyles, /transition:/);
assert.match(dialogStyles, /prefers-reduced-motion: reduce/);

assert.match(skillMarketSource, /<Modal[\s\S]*open=\{installModalOpen\}/);
assert.match(skillMarketSource, /<Drawer[\s\S]*open=\{open\}/);
assert.doesNotMatch(skillMarketSource, /skill-preview-overlay/);
assert.match(cliWizardSource, /<Modal[\s\S]*open=\{open\}/);
assert.match(cliSectionSource, /<CliToolWizard open=\{showWizard\}/);
assert.match(confirmSource, /<Modal/);
assert.match(promptSource, /<Modal/);

console.log('overlay component contract tests passed');
