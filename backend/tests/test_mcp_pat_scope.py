"""TDD test for PAT scope column (Task A1)."""

from app.models.personal_access_token import PersonalAccessToken


def test_pat_model_has_scope_default_read():
    # python-side column default must resolve to "read"
    assert PersonalAccessToken.__table__.c.scope.default.arg == "read"
