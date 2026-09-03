"""Identity provider configuration responses are write-only for credentials."""

from app.api.enterprise_routes_identity import _sanitize_identity_provider_config


def test_provider_config_scrubs_top_level_and_nested_secrets():
    safe = _sanitize_identity_provider_config(
        "oauth2",
        {
            "client_id": "public-client",
            "client_secret": "top-secret",
            "directory": {
                "base_url": "https://directory.example/scim/v2",
                "credentials": {
                    "password": "nested-secret",
                    "private_key": "private-secret",
                },
            },
        },
    )

    assert safe["client_id"] == "public-client"
    assert "client_secret" not in safe
    assert safe["directory"]["base_url"].endswith("/scim/v2")
    assert safe["directory"]["credentials"] == {}
    assert safe["secret_fields_configured"] == [
        "client_secret",
        "directory.credentials.password",
        "directory.credentials.private_key",
    ]
