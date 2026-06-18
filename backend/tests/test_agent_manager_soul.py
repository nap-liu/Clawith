from app.services.agent_manager import replace_or_append_section


def test_replace_existing_section():
    src = "# Soul\n## Personality\nold\n## Boundaries\nb\n"
    out = replace_or_append_section(src, "Personality", "new")
    assert "## Personality\nnew" in out and "old" not in out
    assert "## Boundaries\nb" in out


def test_append_missing_section():
    out = replace_or_append_section("# Soul\n", "Personality", "p")
    assert out.rstrip().endswith("## Personality\np")


def test_empty_content_is_noop():
    assert replace_or_append_section("x", "Personality", "") == "x"
