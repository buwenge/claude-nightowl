"""健康生活页面的离线静态契约测试，不启动服务、不联网。"""

from html.parser import HTMLParser
from pathlib import Path


WELLNESS = Path(__file__).resolve().parent.parent / "wellness"


class _IdsAndLabels(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "label" and attrs.get("for"):
            self.labels.append(attrs["for"])


def test_mobile_page_has_m0_sections_and_accessible_labels():
    html = (WELLNESS / "index.html").read_text(encoding="utf-8")
    for fragment in (
        'lang="zh-CN"',
        'id="deficit-kcal"',
        "计划每日热量缺口",
        'id="entry-form"',
        'id="profile-form"',
        'id="preferences-form"',
        'id="plan-button"',
        'id="entries-list"',
        "今日概览",
        "快速记录",
        "食材偏好",
        "一日三餐",
    ):
        assert fragment in html

    parser = _IdsAndLabels()
    parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids)), "页面 id 必须唯一"
    assert {"entry-type", "meal-text", "exercise-text", "weight-value", "height", "pref-avoid"} <= set(parser.labels)


def test_frontend_uses_the_health_api_contract_and_safe_dom_rendering():
    js = (WELLNESS / "app.js").read_text(encoding="utf-8")
    for fragment in (
        'api("GET", "/profile")',
        'api("PUT", "/profile"',
        'api("GET", "/preferences")',
        'api("PUT", "/preferences"',
        'api("POST", "/days/" + state.date + "/entries"',
        '"/days/" + state.date + "/entries"',
        'api("DELETE", "/days/" + state.date + "/entries/"',
        '"/days/" + state.date + "/summary"',
        '"/days/" + state.date + "/plan"',
        "textContent",
        "encodeURIComponent(id)",
        'var API = "./api"',
        'body.deficit_kcal = deficit',
        'data.calories_in',
        'entry.calories_burned != null',
        'FormData',
        'estimate-kcal',
        'api("GET", "/settings")',
        'api("PUT", "/settings"',
        'range?from=',
        'period-button',
        'archive-bar',
    ):
        assert fragment in js
    assert "innerHTML" not in js
    assert "document.write" not in js


def test_css_keeps_narrow_screen_and_keyboard_affordances():
    css = (WELLNESS / "style.css").read_text(encoding="utf-8")
    for fragment in (
        "@media (max-width:420px)",
        "@media (min-width:620px)",
        "button:focus-visible",
        "touch-action:manipulation",
        "min-height:44px",
    ):
        assert fragment in css


def test_no_remote_assets_or_private_configuration_in_page():
    for path in (WELLNESS / "index.html", WELLNESS / "style.css", WELLNESS / "app.js"):
        source = path.read_text(encoding="utf-8")
        assert "https://" not in source
        assert "http://" not in source
        assert "cdn" not in source.lower()


def test_page_is_intended_for_health_mount_and_uses_same_origin_relative_api():
    html = (WELLNESS / "index.html").read_text(encoding="utf-8")
    js = (WELLNESS / "app.js").read_text(encoding="utf-8")
    assert 'src="./app.js"' in html
    assert 'href="./style.css"' in html
    assert 'var API = "./api"' in js
    assert "/wellness/" not in html + js
    assert 'fetch("http' not in js
