"""Display the serving platform without changing a model's transport identity."""

from urllib.parse import urlsplit


def model_service_platform(provider: str, base_url: str | None) -> str:
    try:
        host = (urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        host = ""
    if host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    } or host.endswith(".maas.aliyuncs.com"):
        return "bailian"
    if host in {
        "tokenhub.tencentmaas.com", "tokenhub-intl.tencentmaas.com",
        "tokenhub.tencentcloudmaas.com", "tokenhub-intl.tencentcloudmaas.com",
    }:
        return "tokenhub"
    if host == "ark.cn-beijing.volces.com":
        return "volcengine"
    return provider
