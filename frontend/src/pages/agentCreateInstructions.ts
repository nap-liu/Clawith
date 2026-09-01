export function buildAgentSetupInstruction(origin: string, apiKey: string): string {
    return `Please create a skill file at skills/platform_sync.md with the following content, then add "Check the platform inbox using the platform_sync skill and process any pending messages" to your HEARTBEAT.md:

---
name: platform_sync
description: Sync with the platform — check inbox, submit results, and send messages.
---

# Platform Sync

## When to use
Check for new messages from the platform during every heartbeat cycle.
You can also proactively send messages to people and agents in your relationships.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: ${origin}/api/gateway/poll
- Header: X-Api-Key: ${apiKey}

The response contains a \`messages\` array. Each message includes:
- \`id\` — unique message ID (use this for reporting)
- \`content\` — the message text
- \`sender_user_name\` — display name of a human sender, when applicable
- \`sender_user_id\` — canonical User UUID of a human sender, when applicable
- \`sender_agent_id\` — canonical Agent UUID of an agent sender, when applicable
- \`conversation_id\` — the conversation this message belongs to
- \`history\` — array of previous messages in this conversation for context

The response also contains a \`relationships\` array describing your colleagues:
- \`display_name\` — display-only person or agent name; never use it for execution
- exactly one of \`user_id\` or \`agent_id\` — the canonical execution target
- \`role\` — relationship type (e.g. collaborator, supervisor)
- \`channels\` — available communication channels (e.g. ["feishu"], ["agent"])

**IMPORTANT**: Use the \`history\` array to understand conversation context before replying.
Use canonical IDs, not display names, to identify recipients.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: ${origin}/api/gateway/report
- Header: X-Api-Key: ${apiKey}
- Header: Content-Type: application/json
- Body: {"message_id": "<id from the message>", "result": "<your response>"}

### 3. Send a message to someone
To proactively contact a person or agent, make an HTTP POST request:
- URL: ${origin}/api/gateway/send-message
- Header: X-Api-Key: ${apiKey}
- Header: X-Idempotency-Key: <stable unique ID for this logical send; reuse the same key only when retrying it>
- Header: Content-Type: application/json
- Human body: {"user_id": "<canonical User UUID>", "channel": "<one available channel>", "content": "<your message>"}
- Agent body: {"agent_id": "<canonical Agent UUID>", "content": "<your message>"}

Provide exactly one of \`user_id\` or \`agent_id\`. For humans with multiple channels, choose one from \`relationships[].channels\`. Always send X-Idempotency-Key and reuse it for retries of the same logical message.
For agents, the reply appears in your next poll.`;
}

export function buildAgentSetupCopyText(origin: string, apiKey: string): string {
    return `Please create a skill file at skills/platform_sync.md, then add "Check the platform inbox using the platform_sync skill and process any pending messages" to HEARTBEAT.md.\n\n---\nname: platform_sync\ndescription: Sync with the platform — check inbox, submit results, and send messages.\n---\n\n# Platform Sync\n\nPoll ${origin}/api/gateway/poll with X-Api-Key: ${apiKey}. Use message history for context. A human sender has sender_user_id; an agent sender has sender_agent_id. Relationships expose display_name for display and exactly one canonical user_id or agent_id for execution. Never execute by display name.\n\nReport each result to ${origin}/api/gateway/report with {"message_id":"<message id>","result":"<response>"}.\n\nSend proactively to ${origin}/api/gateway/send-message. For a human use {"user_id":"<User UUID>","channel":"<relationship channel>","content":"<message>"}; for an agent use {"agent_id":"<Agent UUID>","content":"<message>"}. Provide exactly one recipient ID. If a human has multiple channels, choose one from relationships[].channels. Always include X-Idempotency-Key with one stable unique value per logical send and reuse that same key only for retries.`;
}
