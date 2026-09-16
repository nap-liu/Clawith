import io
import uuid
import zipfile

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.services.skill_upload import read_skill_upload, upload_skill
from tests.test_skill_market import _create_tenant_team
from app.api.skill_management import router
from app.core.security import get_current_user


def package(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


def test_upload_package_normalizes_wrapper_and_rejects_unsafe_files():
    files = read_skill_upload('example.zip', package([
        ('example/SKILL.md', '---\nname: Example\n---\n# Example'),
        ('example/references/guide.md', '# Guide'),
    ]))
    assert {item['path'] for item in files} == {'SKILL.md', 'references/guide.md'}
    for entries in [
        [('SKILL.md', '# Example'), ('../escape.py', 'bad')],
        [('SKILL.md', '# Example'), ('/absolute.py', 'bad')],
        [('SKILL.md', '# Example'), ('SKILL.md', '# Duplicate')],
        [('guide.md', '# No entrypoint')],
        [('SKILL.md', b'\xff\xfe')],
    ]:
        with pytest.raises(HTTPException):
            read_skill_upload('example.zip', package(entries))


@pytest.mark.asyncio(loop_scope='session')
async def test_upload_enforces_ownership_and_does_not_overwrite_existing_skill():
    tenant, admin, member, _ = await _create_tenant_team('upload')
    admin.role = 'org_admin'
    folder = 'upload-' + uuid.uuid4().hex[:12]
    data = b'---\nname: Upload example\ndescription: Uploaded description\n---\n# Example'
    async with async_session() as db:
        with pytest.raises(HTTPException) as denied:
            await upload_skill(db, member, 'SKILL.md', data, folder, 'tenant')
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException):
            await upload_skill(db, admin, 'SKILL.md', data, folder, 'platform')
        result = await upload_skill(db, admin, 'SKILL.md', data, folder, 'tenant')
        skill = await db.get(Skill, uuid.UUID(result['id']))
        assert skill.tenant_id == tenant.id
        assert skill.publisher_user_id == admin.id
        assert skill.status == 'draft'
        assert not skill.is_default
        assert skill.name == 'Upload example'
        with pytest.raises(HTTPException) as conflict:
            await upload_skill(db, admin, 'SKILL.md', b'# Replacement', folder, 'tenant')
        assert conflict.value.status_code == 409
        assert await db.scalar(select(SkillFile.content).where(SkillFile.skill_id == skill.id)) == data.decode()
        admin.role = 'platform_admin'
        result = await upload_skill(db, admin, 'SKILL.md', data, folder + '-global', 'platform')
        assert (await db.get(Skill, uuid.UUID(result['id']))).tenant_id is None
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: admin
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/skills/manage/upload', data={'folder': folder + '-http', 'scope': 'tenant'},
                                     files={'file': ('SKILL.md', data, 'text/markdown')})
        assert response.status_code == 200
        assert response.json()['status'] == 'draft'
