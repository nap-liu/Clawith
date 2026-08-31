"""Feishu message approval and resource transfer methods."""

import httpx

from app.services.feishu_constants import FEISHU_APP_TOKEN_URL, FEISHU_SEND_MSG_URL
from app.services.user_output import sanitize_user_visible_text


class FeishuMessageResourceMethods:
    async def send_approval_card(self, app_id: str, app_secret: str,
                                  creator_open_id: str, agent_name: str,
                                  action_type: str, details: str, approval_id: str) -> dict:
        """Send an interactive approval card to the agent creator via Feishu."""
        import json
        safe_agent_name = sanitize_user_visible_text(agent_name).strip() or "智能体"
        safe_action_type = sanitize_user_visible_text(action_type).strip() or "操作"
        safe_details = sanitize_user_visible_text(details)
        card_content = json.dumps({
            "type": "template",
            "data": {
                "template_id": "",  # Use custom card
                "template_variable": {
                    "agent_name": safe_agent_name,
                    "action_type": safe_action_type,
                    "details": safe_details,
                    "approval_id": approval_id,
                }
            }
        })
        # Simplified — in production, use Feishu interactive card JSON
        text_content = json.dumps({
            "text": (
                f"🔴 {safe_agent_name}: 请求审批\n"
                f"操作: {safe_action_type}\n详情: {safe_details}\n\n请在平台审批。"
            )
        })
        return await self.send_message(app_id, app_secret, creator_open_id, "text", text_content)

    async def download_message_resource(self, app_id: str, app_secret: str,
                                         message_id: str, file_key: str,
                                         resource_type: str = "file") -> bytes:
        """Download a file or image from a Feishu message.

        Args:
            resource_type: "file" or "image"
        Returns raw file bytes.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            resp.raise_for_status()
            return resp.content

    async def upload_and_send_file(self, app_id: str, app_secret: str,
                                    receive_id: str, file_path,
                                    receive_id_type: str = "open_id",
                                    accompany_msg: str = "",
                                    on_result=None) -> dict:
        """Upload a local file to Feishu and send it as a file message.

        Returns the send_message response dict.
        """
        import json as _json
        from pathlib import Path as _Path
        fp = _Path(file_path)
        async with httpx.AsyncClient(timeout=60) as client:
            # Get token
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id, "app_secret": app_secret,
            })
            token_data = self._parse_api_response(token_resp, stage="file_token")
            app_token = token_data.get("app_access_token", "")
            if not app_token:
                raise RuntimeError("Feishu file token response missing app_access_token")
            headers = {"Authorization": f"Bearer {app_token}"}

            # Upload file
            with open(fp, "rb") as f:
                file_bytes = f.read()
            # Determine file type for Feishu upload
            ext = fp.suffix.lower()
            feishu_file_type = "stream"  # generic binary
            if ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md"):
                feishu_file_type = "stream"
            upload_resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                files={"file": (fp.name, file_bytes, "application/octet-stream")},
                data={"file_type": feishu_file_type, "file_name": fp.name},
                headers=headers,
            )
            upload_data = self._parse_api_response(upload_resp, stage="file_upload")
            file_key = upload_data["data"]["file_key"]

            # Send text accompany message first if provided
            if accompany_msg:
                text_resp = await client.post(
                    f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                    json={"receive_id": receive_id, "msg_type": "text",
                          "content": _json.dumps({"text": accompany_msg})},
                    headers=headers,
                )
                text_data = self._parse_api_response(
                    text_resp,
                    stage="file_caption",
                )
                if on_result is not None:
                    await on_result("file_caption", text_data)

            # Send file message
            resp = await client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={"receive_id": receive_id, "msg_type": "file",
                      "content": _json.dumps({"file_key": file_key})},
                headers=headers,
            )
            result = self._parse_api_response(resp, stage="file_message")
            if on_result is not None:
                await on_result("channel_file", result)
            return result

