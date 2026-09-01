"""Feishu Bitable, Docs, and Approval API methods."""

import httpx


class FeishuProductAPIMethods:
    # --- Bitable (多维表格) API ---

    async def bitable_list_tables(self, app_id: str, app_secret: str, app_token: str) -> dict:
        """List all tables in a Bitable app."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def bitable_list_fields(self, app_id: str, app_secret: str, app_token: str, table_id: str) -> dict:
        """List all fields in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def bitable_query_records(self, app_id: str, app_secret: str, app_token: str, table_id: str, filters: dict | None = None) -> dict:
        """Query records in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body = {}
        if filters:
            body = filters
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def bitable_create_record(self, app_id: str, app_secret: str, app_token: str, table_id: str, fields: dict) -> dict:
        """Create a new record in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                json={"fields": fields},
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def bitable_update_record(self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str, fields: dict) -> dict:
        """Update an existing record in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                json={"fields": fields},
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def bitable_delete_record(self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str) -> dict:
        """Delete an existing record in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def bitable_create_app(self, app_id: str, app_secret: str, name: str, folder_token: str = "") -> dict:
        """Create a new Bitable (多维表格) app.

        Uses the Bitable v1 apps API: POST /open-apis/bitable/v1/apps
        If folder_token is empty, the file is created in the root 'My Drive'.

        Args:
            name:         The display name of the new Bitable (max 255 chars).
            folder_token: Parent folder token (optional). Leave empty for root.
        Returns:
            API response dict containing 'data.app.app_token' as the new app_token.
        """
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: dict = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/bitable/v1/apps",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return resp.json()


    # --- Docs API ---
    async def read_feishu_doc(self, app_id: str, app_secret: str, document_id: str) -> dict:
        """Get pure text content of a new-version Feishu Doc (docx)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/raw_content",
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def create_feishu_doc(self, app_id: str, app_secret: str, folder_token: str | None = None, title: str = "Untitled Document") -> dict:
        """Create a new Feishu Doc (docx)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def append_feishu_doc(self, app_id: str, app_secret: str, document_id: str, content: str) -> dict:
        """Append text to the end of a Feishu Doc (document_id is also the root block_id)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        # Convert plain text to a text block
        body = {
            "children": [
                {
                    "block_type": 2, # Text block (paragraph)
                    "text": {
                        "elements": [
                            {
                                "text_run": {
                                    "content": content
                                }
                            }
                        ]
                    }
                }
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def append_feishu_doc_blocks(self, app_id: str, app_secret: str, document_id: str, block_id: str, blocks: list) -> dict:
        """Append pre-parsed Markdown blocks to a Feishu doc block (e.g., body_block_id)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children",
                json={"children": blocks},
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    # --- Approval API ---
    async def create_approval_instance(self, app_id: str, app_secret: str, approval_code: str, user_id: str, form_data: str) -> dict:
        """Create a Feishu approval instance."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body = {
            "approval_code": approval_code,
            "user_id": user_id,
            "form": form_data
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def query_approval_instances(self, app_id: str, app_secret: str, approval_code: str, status: str = None) -> dict:
        """Query Feishu approval instances."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body = {"approval_code": approval_code}
        if status:
            body["status"] = status
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()

    async def get_approval_instance(self, app_id: str, app_secret: str, instance_id: str) -> dict:
        """Get details of a specific Feishu approval instance."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_id}",
                headers={"Authorization": f"Bearer {tenant_token}"}
            )
            return resp.json()
