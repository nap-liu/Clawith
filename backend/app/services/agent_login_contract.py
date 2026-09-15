"""One schema shared by builtin seeding and the fallback catalog."""

AGENT_LOGIN_SEED = {
    "name": "create_published_page_login_link",
    "display_name": "Generate Temporary Access Link",
    "description": (
        "Create an INTERNAL login URL for your own browser to read a published report as the current human "
        "conversation participant. Pass a report URL or short ID. The URL expires after 5 minutes and can "
        "be redeemed once; the resulting login lasts 1 hour. Reopening it in the same browser with its "
        "still-valid login reuses that login without extending expiry. Report permissions are checked "
        "normally when the browser opens the report, not during issuance. Open the URL using your browser "
        "tools. Never send, quote, embed, or otherwise expose this INTERNAL URL or its credentials to any "
        "user or external recipient. Do not use a browser shared with other users."
    ),
    "category": "pages", "icon": "🔐", "is_default": False,
    "parameters_schema": {
        "type": "object", "properties": {"page_url": {
            "type": "string", "description": "Published report URL, /p/short_id path, or short_id.",
        }}, "required": ["page_url"], "additionalProperties": False,
    },
    "config": {}, "config_schema": {},
}
