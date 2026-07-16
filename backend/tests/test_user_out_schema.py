import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.schemas.schemas import UserOut


def test_user_out_accepts_directory_only_user_without_identity_verification_state():
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=None,
        username=None,
        email=None,
        display_name="Directory Member",
        avatar_url=None,
        role="member",
        tenant_id=uuid.uuid4(),
        title=None,
        primary_mobile=None,
        registration_source="dingtalk",
        is_active=True,
        email_verified=None,
        created_at=datetime.now(timezone.utc),
    )

    result = UserOut.model_validate(user)

    assert result.email_verified is True


def test_user_out_preserves_explicit_identity_verification_state():
    base = {
        "id": uuid.uuid4(),
        "display_name": "Login User",
        "role": "member",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }

    assert UserOut.model_validate({**base, "email_verified": False}).email_verified is False
    assert UserOut.model_validate({**base, "email_verified": True}).email_verified is True
