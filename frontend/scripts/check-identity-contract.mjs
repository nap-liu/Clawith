import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..', '..');
const read = (relativePath) => readFileSync(resolve(root, relativePath), 'utf8');
const detail = read('frontend/src/pages/agent-detail/AgentDetailPage.tsx');
const create = read('frontend/src/pages/AgentCreate.tsx');
const modal = read('frontend/src/components/CustomAgentModal.tsx');
const gateway = read('backend/app/api/gateway.py');

const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

assert(!detail.includes('m.participant_id'), 'history must not expose participant_id');
assert(
  detail.split('m.sender_agent_id && { sender_agent_id: m.sender_agent_id }').length - 1 === 2,
  'initial and paginated history must preserve sender_agent_id',
);
assert(
  detail.split('m.sender_user_id && { sender_user_id: m.sender_user_id }').length - 1 === 2,
  'initial and paginated history must preserve sender_user_id',
);
assert(
  detail.includes('d.sender_user_id || d.user_id'),
  'live IM messages must preserve canonical sender_user_id',
);
assert(!modal.includes('"target": "<name of person or agent>"'), 'modal must not execute by name');
for (const [name, instruction] of [
  ['AgentCreate', create],
  ['CustomAgentModal', modal],
  ['backend setup guide', gateway],
]) {
  assert(instruction.includes('X-Idempotency-Key'), `${name} must instruct idempotent retries`);
  assert(instruction.includes('user_id'), `${name} must document canonical user_id`);
  assert(instruction.includes('agent_id'), `${name} must document canonical agent_id`);
}

console.log('Identity contract checks passed.');
