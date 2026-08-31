"""Low-level Microsoft Teams activity delivery."""

import httpx
from loguru import logger


async def _send_teams_message_single_chunk(
    access_token: str,
    service_url: str,
    conversation_id: str,
    activity: dict,
) -> dict:
    """Send a single chunked message to Microsoft Teams."""
    # Ensure service_url doesn't have trailing slash to avoid double slashes
    service_url_clean = service_url.rstrip("/")
    post_url = f"{service_url_clean}/v3/conversations/{conversation_id}/activities"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(post_url, headers=headers, json=activity)
            if resp.status_code != 200:
                error_body = resp.text
                try:
                    error_json = resp.json()
                    error_description = error_json.get("error", {}).get("message", error_json.get("message", "No description"))
                    error_code = error_json.get("error", {}).get("code", "unknown")
                    logger.error(f"Teams: Failed to send message: status={resp.status_code}, error={error_code}, description={error_description}")
                except:
                    logger.error(f"Teams: Failed to send message: status={resp.status_code}, response={error_body[:500]}")
                logger.error(f"Teams: POST URL={post_url}, conversation_id={conversation_id}, service_url={service_url}")
            resp.raise_for_status()
            logger.info(f"Teams: Sent message to conversation {conversation_id}")
            try:
                return resp.json()
            except ValueError:
                return {}
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, 'response') and e.response else "No response body"
        try:
            if hasattr(e, 'response') and e.response:
                error_json = e.response.json()
                error_description = error_json.get("error", {}).get("message", error_json.get("message", "No description"))
                error_code = error_json.get("error", {}).get("code", "unknown")
                logger.error(f"Teams: HTTP error sending message: status={e.response.status_code}, error={error_code}, description={error_description}")
        except:
            logger.error(f"Teams: HTTP error sending message: status={e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}, response={error_body[:500]}")
        logger.error(f"Teams: POST URL={post_url}, conversation_id={conversation_id}, service_url={service_url}")
        raise
