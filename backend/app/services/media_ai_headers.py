"""Resolve enterprise media headers only for the configured provider origin."""

from app.services.llm.provider_parameters import merge_request_headers
from app.services.model_headers import default_model_headers


def extra_headers(config: dict) -> dict:
    headers = config.get("extra_headers")
    return dict(headers) if headers is not None else default_model_headers(
        config.get("provider", ""), config.get("base_url"),
    )


def provider_headers(config: dict) -> dict:
    return merge_request_headers({"Authorization": f"Bearer {config['api_key']}"}, extra_headers(config))
