import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { loadTypeScriptModule } from './load-typescript-module.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const { parseExactRichMentions } = loadTypeScriptModule(resolve(
    __dirname,
    '../src/components/ui/richMentionParsing.ts',
));

const options = [
    { value: 'agent-rd', label: '研发工程师' },
    { value: 'agent-qa', label: '测试专员' },
    { value: 'agent-short', label: '研发' },
    { value: 'agent-disabled', label: '停用成员', enabled: false },
];

{
    const parsed = parseExactRichMentions('请@研发工程师 处理这个问题', options);
    assert.equal(parsed.length, 1);
    assert.equal(parsed[0].agentId, 'agent-rd');
    assert.equal(parsed[0].raw, '@研发工程师');
}

{
    const parsed = parseExactRichMentions('@研发工程师，@测试专员 请分别处理', options);
    assert.deepEqual(Array.from(parsed, (mention) => mention.agentId), ['agent-rd', 'agent-qa']);
}

{
    const parsed = parseExactRichMentions('@研发工程师 @研发工程师', options);
    assert.equal(parsed.length, 2);
    assert.equal(parsed.every((mention) => mention.agentId === 'agent-rd'), true);
}

{
    const [prefix] = parseExactRichMentions('@研发 只是完整名字的前缀', [
        { value: 'agent-rd', label: '研发工程师' },
    ]);
    assert.equal(prefix.status, 'unresolved');
    assert.equal(prefix.reason, 'unmatched');
    const [longerPrefix] = parseExactRichMentions('@研发团队 不是研发', options);
    assert.equal(longerPrefix.status, 'unresolved');
}

{
    const ambiguous = parseExactRichMentions('@测试专员 处理', [
        ...options,
        { value: 'agent-qa-2', label: '测试专员' },
    ])[0];
    assert.equal(ambiguous.status, 'unresolved');
    assert.equal(ambiguous.reason, 'ambiguous');
}

{
    const unmatched = parseExactRichMentions('请 @不存在 和 @停用成员 处理', options);
    assert.deepEqual(Array.from(unmatched, (mention) => mention.reason), ['unmatched', 'unavailable']);
}

{
    const pasted = parseExactRichMentions('批量：@研发工程师\n@不存在，@测试专员。', options);
    assert.deepEqual(Array.from(pasted, (mention) => mention.status), ['resolved', 'unresolved', 'resolved']);
    assert.equal(pasted[1].agentId, undefined);
}

{
    assert.equal(parseExactRichMentions('dev@example.com foo@研发工程师', options).length, 0);
}

console.log('rich mention parser tests passed');
