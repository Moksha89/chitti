from chitti.main import humanize_belief_key, project_from_brief, render_markdown


def test_markdown_output_formats_code_and_escapes_html() -> None:
    rendered = render_markdown(
        "**bold**\n\n`inline()`\n\n```python\nprint('<unsafe>')\n```\n\n<script>alert('x')</script>"
    )

    assert "<strong>bold</strong>" in rendered
    assert "<code>inline()</code>" in rendered
    assert "<pre><code class=\"language-python\">" in rendered
    assert "&lt;unsafe&gt;" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_belief_presentation_keeps_keys_consistent_and_values_readable() -> None:
    assert humanize_belief_key("hard_rules_meeting_start_time") == "Hard rules meeting start time"
    assert humanize_belief_key("hard_rules.meeting_start_time") == "Hard rules meeting start time"


def test_project_brief_is_detected_without_starting_execution() -> None:
    assert project_from_brief("VSports", True) == "vsports"
    assert project_from_brief("VSports", False) is None
    assert project_from_brief(None, True) is None
    assert project_from_brief("!!!", True) is None
