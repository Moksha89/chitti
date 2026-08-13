from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def test_brand_profile_removal_form_renders_inside_brand_language_panel() -> None:
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates")
    )
    html = templates.get_template("dashboard.html").render(
        csrf_token="csrf",
        namespace="general",
        namespace_options=[],
        greeting="Welcome",
        owner_name="owner",
        display_timezone="UTC",
        briefing={"local_date": "today", "timezone": "UTC", "content": ""},
        notifications=[],
        runner_health=[],
        reminders=[],
        brand_profile={
            "brand_colors": ["TRIAL TEAL"],
            "typography": "FreeSans",
            "poster_formats": ["1080x1350"],
            "audience": "trial observers",
            "voice": "trial voice",
            "do_not_use": ["production approval"],
            "updated_by": "owner",
            "updated_at": "now",
        },
        brand_error=None,
        available_fonts=["FreeSans"],
        conflicts=[],
        decisions=[],
        plans=[],
        transcript=[],
    )
    heading = html.index("<h2>Brand language</h2>")
    panel_start = html.rfind('<section class="panel"', 0, heading)
    panel_end = html.index("</section>", heading)
    removal_form = html.index('<form method="post" action="/brand-profile/remove">')
    assert panel_start < removal_form < panel_end
    assert html.count('action="/brand-profile/remove"') == 1
    assert html.index("The removal is recorded in profile history.") > removal_form
