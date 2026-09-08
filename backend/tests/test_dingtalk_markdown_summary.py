from app.services import dingtalk_service


def test_dingtalk_markdown_never_uses_notification_placeholder():
    payload = dingtalk_service.build_dingtalk_markdown_content("![执行结果](https://example.com/result.png)")

    assert payload == {
        "title": "执行结果",
        "text": "![执行结果](https://example.com/result.png)",
    }
