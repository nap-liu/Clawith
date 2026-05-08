"""Unit tests for the sender_attribution wrap helper."""

import uuid

from app.services.sender_attribution import wrap_with_sender


def test_wraps_basic_message():
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    out = wrap_with_sender("hello world", uid, "Alice")
    assert out == ('<sender id="550e8400-e29b-41d4-a716-446655440000">Alice</sender>\nhello world')


def test_escapes_xml_special_chars_in_name():
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    out = wrap_with_sender("hi", uid, "A<b>&c")
    assert out == ('<sender id="550e8400-e29b-41d4-a716-446655440000">A&lt;b&gt;&amp;c</sender>\nhi')


def test_neutralizes_close_tag_inside_name():
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    out = wrap_with_sender("hi", uid, "</sender>X")
    # </sender> in name must be entity-escaped so the outer tag still closes correctly
    assert out == ('<sender id="550e8400-e29b-41d4-a716-446655440000">&lt;/sender&gt;X</sender>\nhi')


def test_collapses_control_chars_in_name():
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    out = wrap_with_sender("hi", uid, "Line1\nLine2\rLine3\tTab")
    # \n \r \t become spaces so the tag stays on a single line
    assert '<sender id="550e8400-e29b-41d4-a716-446655440000">Line1 Line2 Line3 Tab</sender>\nhi' == out


def test_content_is_left_untouched():
    """Content (after the tag) is plain text — must not be escaped or modified."""
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    raw = 'Look at <sender id="fake">Bob</sender> — also & < > stay as-is\nmulti-line ok'
    out = wrap_with_sender(raw, uid, "Alice")
    assert out.endswith("\n" + raw)


def test_user_id_none_returns_content_unchanged():
    out = wrap_with_sender("hello", None, "Alice")
    assert out == "hello"


def test_display_name_none_falls_back_to_unknown():
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    out = wrap_with_sender("hi", uid, None)
    assert out == ('<sender id="550e8400-e29b-41d4-a716-446655440000">Unknown</sender>\nhi')


def test_display_name_empty_string_falls_back_to_unknown():
    uid = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
    out = wrap_with_sender("hi", uid, "")
    assert out == ('<sender id="550e8400-e29b-41d4-a716-446655440000">Unknown</sender>\nhi')


def test_string_user_id_works():
    """user_id may be a uuid string (not only UUID object)."""
    out = wrap_with_sender("hi", "550e8400-e29b-41d4-a716-446655440000", "Alice")
    assert out == ('<sender id="550e8400-e29b-41d4-a716-446655440000">Alice</sender>\nhi')
