import pytest

from app.services.im_text import extract_plain_text_summary, project_nonempty_message_summary


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "## 发布结果\n\n**服务已更新**，查看[发布记录](https://example.com)",
            "发布结果 服务已更新，查看发布记录",
        ),
        ("![执行结果](https://example.com/result.png)", "执行结果"),
        ("![](https://example.com/result.png)", ""),
        ("![](https://example.com/a(b)?token=secret)", ""),
        ("[文档](https://example.com/a(b).html)", "文档"),
        (r"[文档](https://example.com/a\(b\).html)", "文档"),
        ('<img src="result.png" alt="执行结果">', "执行结果"),
        ("`<status>ready</status>`", "<status>ready</status>"),
        ("<p>任务完成 &amp; 已验证</p><script>ignore()</script>", "任务完成 & 已验证"),
        ("- 第一项\n- 第二项 😀", "第一项 第二项 😀"),
        ("- [ ] 待办事项\n- [x] 已完成事项", "待办事项 已完成事项"),
        ("> 第一层\n> > 第二层", "第一层 第二层"),
        ("开始\n---\n结束", "开始 结束"),
        ("| 姓名 | 城市 |\n|---|---|\n| 张三 | 北京 |", "姓名 城市 张三 北京"),
        ("姓名 | 城市\n--- | ---\n张三 | 北京", "姓名 城市 张三 北京"),
        ("普通正文 A | B 必须保留", "普通正文 A | B 必须保留"),
        ("查看[发布记录][release]\n\n[release]: https://example.com", "查看发布记录"),
        ("访问 <https://example.com/path>", "访问 https://example.com/path"),
        ("变量 snake_case 和 2 * 3 保持原样", "变量 snake_case 和 2 * 3 保持原样"),
    ],
)
def test_extract_plain_text_summary(source, expected):
    assert extract_plain_text_summary(source) == expected


def test_extract_plain_text_summary_truncates_deterministically():
    assert extract_plain_text_summary("一二三四五六", max_chars=5) == "一二三四…"
    assert extract_plain_text_summary("abc", max_chars=1) == "…"


def test_extract_plain_text_summary_returns_empty_for_content_without_visible_text():
    assert extract_plain_text_summary("<style>hidden</style><!-- hidden -->") == ""
    assert extract_plain_text_summary("<head><title>hidden</title></head>") == ""
    assert extract_plain_text_summary("<template>hidden</template><noscript>hidden</noscript>") == ""
    assert extract_plain_text_summary("<head>before</title>SECRET</head>") == ""


def test_extract_plain_text_summary_decodes_html_once_and_preserves_code_entities():
    assert extract_plain_text_summary("<p>&amp;lt;</p>") == "&lt;"
    assert extract_plain_text_summary("`&lt;status&gt;`") == "&lt;status&gt;"


def test_extract_plain_text_summary_placeholder_cannot_collide_with_input():
    source = "\ue000IMCODE0\ue001 and `<status>ready</status>`"
    assert extract_plain_text_summary(source) == "\ue000IMCODE0\ue001 and <status>ready</status>"


def test_nonempty_projection_preserves_delivery_for_format_only_content():
    assert project_nonempty_message_summary("---") == "非文本消息"
    assert project_nonempty_message_summary("<style>hidden</style>") == "非文本消息"
    assert project_nonempty_message_summary("![](https://example.com/signed?token=secret)") == "非文本消息"


def test_nonempty_projection_rejects_only_empty_source():
    with pytest.raises(ValueError, match="must not be empty"):
        project_nonempty_message_summary("  \n  ")


def test_extract_plain_text_summary_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="positive"):
        extract_plain_text_summary("text", max_chars=0)
