"""Round-trip tests for env-value encryption."""

from __future__ import annotations

from app.services.cli_tools.crypto import decrypt_env, encrypt_env, mask_env


def test_encrypt_env_round_trip():
    plaintext = {"API_KEY": "secret-abc", "PUBLIC_URL": "https://example.com"}
    encrypted = encrypt_env(plaintext)
    assert encrypted != plaintext
    for v in encrypted.values():
        assert v.startswith("enc:v1:")
    assert decrypt_env(encrypted) == plaintext


def test_decrypt_passes_through_unencrypted_values():
    # Older records may predate encryption; the codec must tolerate plaintext.
    assert decrypt_env({"OLD": "plain-value"}) == {"OLD": "plain-value"}


def test_mask_env_hides_values():
    plain = {"API_KEY": "secret", "URL": "https://x"}
    assert mask_env(plain) == {"API_KEY": "***", "URL": "***"}


def test_encrypt_env_handles_empty_dict():
    assert encrypt_env({}) == {}
    assert decrypt_env({}) == {}
    assert mask_env({}) == {}
