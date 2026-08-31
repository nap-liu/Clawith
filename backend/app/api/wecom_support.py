"""Shared token and encryption helpers for the WeCom API."""

import base64
import hashlib
import os
import struct

from Crypto.Cipher import AES


async def _get_wecom_token_cached(corp_id: str, corp_secret: str) -> str:
    """Get WeCom access_token with Redis (preferred) + memory fallback caching.

    Key: clawith:token:wecom:{corp_id}
    TTL: 6900s (7200s validity - 5 min early refresh)
    """
    from app.core.token_cache import get_cached_token, set_cached_token
    import httpx as _httpx

    cache_key = f"clawith:token:wecom:{corp_id}"
    cached = await get_cached_token(cache_key)
    if cached:
        return cached

    async with _httpx.AsyncClient(timeout=10) as _client:
        _resp = await _client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": corp_id, "corpsecret": corp_secret},
        )
        _data = _resp.json()
        token = _data.get("access_token", "")
        expires_in = int(_data.get("expires_in") or 7200)
        if token:
            ttl = max(expires_in - 300, 300)
            await set_cached_token(cache_key, token, ttl)
        return token


def _pad(text: bytes) -> bytes:
    """PKCS7 padding for AES-CBC."""
    BLOCK_SIZE = 32
    pad_len = BLOCK_SIZE - (len(text) % BLOCK_SIZE)
    return text + bytes([pad_len] * pad_len)


def _unpad(text: bytes) -> bytes:
    """Remove PKCS7 padding."""
    pad_len = text[-1]
    return text[:-pad_len]


def _decrypt_msg(encrypt_key: str, encrypted_text: str) -> tuple[str, str]:
    """Decrypt a WeCom encrypted message.

    Returns (decrypted_xml, corp_id)
    """
    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted = _unpad(cipher.decrypt(base64.b64decode(encrypted_text)))
    # Skip 16 random bytes, then 4 bytes msg_length (network order)
    msg_len = struct.unpack("!I", decrypted[16:20])[0]
    msg_content = decrypted[20:20 + msg_len].decode("utf-8")
    corp_id = decrypted[20 + msg_len:].decode("utf-8")
    return msg_content, corp_id


def _encrypt_msg(encrypt_key: str, reply_msg: str, corp_id: str) -> str:
    """Encrypt a reply message for WeCom."""
    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    msg_bytes = reply_msg.encode("utf-8")
    buf = os.urandom(16) + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(_pad(buf))
    return base64.b64encode(encrypted).decode("utf-8")


def _verify_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Generate WeCom message signature."""
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()
