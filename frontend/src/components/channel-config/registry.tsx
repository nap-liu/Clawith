import type { ChannelDef } from './types';

const SlackIcon = <img src="/slack.png" alt="Slack" width="20" height="20" style={{ borderRadius: '4px' }} />;
const DiscordIcon = <img src="/discord.png" alt="Discord" width="20" height="20" style={{ borderRadius: '4px' }} />;
const FeishuIcon = <img src="/feishu.png" alt="Feishu" width="20" height="20" style={{ borderRadius: '4px' }} />;
const TeamsIcon = <img src="/teams.png" alt="Teams" width="20" height="20" style={{ borderRadius: '4px' }} />;
const WeComIcon = <img src="/wecom.png" alt="WeCom" width="20" height="20" style={{ borderRadius: '4px' }} />;
const WeChatIcon = <img src="/wechat.svg" alt="WeChat" width="20" height="20" style={{ borderRadius: '4px' }} />;
const DingTalkIcon = <img src="/dingtalk.png" alt="DingTalk" width="20" height="20" style={{ borderRadius: '4px' }} />;
const AtlassianIcon = <img src="/atlassian.png" alt="Atlassian" width="20" height="20" style={{ borderRadius: '4px' }} />;

export const CHANNEL_REGISTRY: ChannelDef[] = [
    {
        id: 'slack',
        icon: SlackIcon,
        nameKey: 'common.channels.slack',
        nameFallback: 'Slack',
        desc: 'Slack Bot',
        apiSlug: 'slack-channel',
        fields: [
            { key: 'bot_token', label: 'Bot Token', placeholder: 'xoxb-...', type: 'password', required: true },
            { key: 'signing_secret', label: 'Signing Secret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.slack', steps: 8 },
        webhookLabel: 'Webhook URL (Event Subscriptions URL)',
    },
    {
        id: 'discord',
        icon: DiscordIcon,
        nameKey: 'common.channels.discord',
        nameFallback: 'Discord',
        desc: 'Gateway / Webhook',
        apiSlug: 'discord-channel',
        connectionMode: true,
        fields: [
            { key: 'application_id', label: 'Application ID', placeholder: '1234567890', required: true },
            { key: 'bot_token', label: 'Bot Token', type: 'password', required: true },
            { key: 'public_key', label: 'Public Key', required: true },
        ],
        wsFields: [
            { key: 'bot_token', label: 'Bot Token', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.discord', steps: 7 },
        wsGuide: { prefix: 'channelGuide.discord', steps: 4 },
        webhookLabel: 'Interactions Endpoint URL',
    },
    {
        id: 'teams',
        icon: TeamsIcon,
        nameKey: 'common.channels.teams',
        nameFallback: 'Microsoft Teams',
        desc: 'Teams Bot',
        apiSlug: 'teams-channel',
        fields: [
            { key: 'app_id', label: 'App ID (Client ID)', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', required: true },
            { key: 'app_secret', label: 'App Secret (Client Secret)', type: 'password', required: true },
            { key: 'tenant_id', label: 'channelGuide.teams.tenantId', placeholder: 'channelGuide.teams.tenantIdPlaceholder' },
        ],
        guide: { prefix: 'channelGuide.teams', steps: 5 },
        webhookLabel: 'Messaging Endpoint URL',
    },
    {
        id: 'feishu',
        icon: FeishuIcon,
        nameKey: 'agent.settings.channel.feishu',
        nameFallback: 'Feishu / Lark',
        desc: 'Feishu / Lark',
        useChannelApi: true,
        connectionMode: true,
        fields: [
            { key: 'app_id', label: 'App ID', placeholder: 'cli_xxxxxxxxxxxxxxxx', required: true },
            { key: 'app_secret', label: 'App Secret', type: 'password', required: true },
            { key: 'encrypt_key', label: 'Encrypt Key', type: 'password' },
        ],
        guide: { prefix: 'channelGuide.feishu', steps: 8 },
        wsGuide: { prefix: 'channelGuide.feishu', steps: 8 },
        showPermJson: true,
        webhookLabel: 'Webhook URL',
    },
    {
        id: 'wechat',
        icon: WeChatIcon,
        nameKey: 'common.channels.wechat',
        nameFallback: 'WeChat',
        desc: 'WeChat iLink Bot',
        apiSlug: 'wechat-channel',
        editOnly: true,
        fields: [],
        guide: { prefix: 'channelGuide.wechat', steps: 4 },
    },
    {
        id: 'wecom',
        icon: WeComIcon,
        nameKey: 'common.channels.wecom',
        nameFallback: 'WeCom',
        desc: 'WebSocket / Webhook',
        apiSlug: 'wecom-channel',
        connectionMode: true,
        fields: [
            { key: 'corp_id', label: 'CorpID', required: true },
            { key: 'wecom_agent_id', label: 'AgentID', required: true },
            { key: 'secret', label: 'Secret', type: 'password', required: true },
            { key: 'token', label: 'Token', required: true },
            { key: 'encoding_aes_key', label: 'EncodingAESKey', required: true },
        ],
        wsFields: [
            { key: 'bot_id', label: 'Bot ID', placeholder: 'aibXXXXXXXXXXXX', required: true },
            { key: 'bot_secret', label: 'Bot Secret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.wecom', steps: 6 },
        wsGuide: { prefix: 'channelGuide.wecom', steps: 6 },
        webhookLabel: 'Webhook URL',
    },
    {
        id: 'dingtalk',
        icon: DingTalkIcon,
        nameKey: 'common.channels.dingtalk',
        nameFallback: 'DingTalk',
        desc: 'Stream Mode',
        apiSlug: 'dingtalk-channel',
        connectionMode: true,
        fields: [
            { key: 'app_key', label: 'AppKey', type: 'password', required: true },
            { key: 'app_secret', label: 'AppSecret', type: 'password', required: true },
            { key: 'agent_id', label: 'AgentId', type: 'text', placeholder: 'DingTalk应用AgentId(可选)', required: false },
        ],
        guide: { prefix: 'channelGuide.dingtalk', steps: 6 },
        webhookLabel: 'Webhook URL',
    },
    {
        id: 'atlassian',
        icon: AtlassianIcon,
        nameKey: 'common.channels.atlassian',
        nameFallback: 'Atlassian',
        desc: 'Jira / Confluence / Compass (Rovo MCP)',
        apiSlug: 'atlassian-channel',
        hasTestConnection: true,
        fields: [
            { key: 'api_key', label: 'API Key', type: 'password', required: true },
            { key: 'cloud_id', label: 'Cloud ID', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' },
        ],
        guide: { prefix: 'channelGuide.atlassian', steps: 5 },
    },
];

export const FEISHU_PERM_BASIC_JSON = '{"scopes":{"tenant":["contact:contact.base:readonly","contact:user.base:readonly","contact:user.employee_id:readonly","contact:user.id:readonly","im:chat","im:message","im:message.group_at_msg:readonly","im:message.p2p_msg:readonly","im:message:send_as_bot","im:resource"],"user":[]}}';

export const FEISHU_PERM_BASIC_DISPLAY = `{
  "scopes": {
    "tenant": [
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "contact:user.id:readonly",
      "im:chat",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.p2p_msg:readonly",
      "im:message:send_as_bot",
      "im:resource"
    ],
    "user": []
  }
}`;

export const FEISHU_PERM_FULL_JSON = '{"scopes":{"tenant":["approval:approval","base:app:create","base:dashboard:create","base:field_group:create","bitable:app","bitable:app:readonly","board:whiteboard:node:create","calendar:calendar.event:create","calendar:calendar.event:delete","calendar:calendar.event:read","calendar:calendar.event:update","calendar:calendar.free_busy:read","calendar:calendar:readonly","contact:contact.base:readonly","contact:user.base:readonly","contact:user.employee_id:readonly","contact:user.id:readonly","docx:document","docx:document:create","drive:drive","im:chat","im:message","im:message.group_at_msg:readonly","im:message.p2p_msg:readonly","im:message:send_as_bot","im:resource","sheets:spreadsheet:create","slides:presentation:create","slides:presentation:write_only","wiki:wiki","wiki:wiki:readonly"],"user":[]}}';

export const FEISHU_PERM_FULL_DISPLAY = `{
  "scopes": {
    "tenant": [
      "approval:approval",
      "base:app:create",
      "base:dashboard:create",
      "base:field_group:create",
      "bitable:app",
      "bitable:app:readonly",
      "board:whiteboard:node:create",
      "calendar:calendar.event:create",
      "calendar:calendar.event:delete",
      "calendar:calendar.event:read",
      "calendar:calendar.event:update",
      "calendar:calendar.free_busy:read",
      "calendar:calendar:readonly",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "contact:user.id:readonly",
      "docx:document",
      "docx:document:create",
      "drive:drive",
      "im:chat",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.p2p_msg:readonly",
      "im:message:send_as_bot",
      "im:resource",
      "sheets:spreadsheet:create",
      "slides:presentation:create",
      "slides:presentation:write_only",
      "wiki:wiki",
      "wiki:wiki:readonly"
    ],
    "user": []
  }
}`;

export const EyeOpen = <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>;
export const EyeClosed = <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94" /><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19" /><line x1="1" y1="1" x2="23" y2="23" /></svg>;
