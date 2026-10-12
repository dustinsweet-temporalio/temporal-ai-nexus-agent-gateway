"""The release console's visual rules: token contrast, no stray colors, vendored assets, stable IDs.

Print the contrast table: uv run python -m tests.test_console_style
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "console" / "web"
CSS = (WEB / "styles.css").read_text()
JS = (WEB / "app.js").read_text()
HTML = (WEB / "index.html").read_text()

NEUTRAL_TEXT = ["text", "body", "secondary"]
ACCENTS = ["violet", "sky", "orange", "pink", "yellow", "green", "coral"]
SURFACES = ["bg", "surface", "raised"]
TINT = 0.12  # accent at 12 percent over --surface
MINIMUM = 4.5

STABLE_IDS = [
    "registry-panel", "registry-row-", "policy-panel", "policy-rule-", "blocked-releases", "pipeline",
    "stage-", "calls-diagram", "node-agent", "node-gateway", "node-", "focus-panel", "focus-approval",
    "focus-review", "focus-decision", "focus-blocked-endpoint", "focus-circuit-breaker", "focus-idle",
    "toast-refusal", "worker-", "audit-bar", "audit-table", "cost-meter",
]  # fmt: skip
ZOOM_TARGETS = ["topbar", "workers", "registry", "policy", "pipeline", "calls", "focus", "audit"]


def token_block() -> str:
    match = re.search(r":root\s*\{(.*?)\}", CSS, re.DOTALL)
    assert match, "styles.css starts with a :root token block"
    return match.group(1)


def tokens() -> dict[str, str]:
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})\s*;", token_block()))


def rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def luminance(color: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(
    top: tuple[float, float, float], base: tuple[float, float, float], amount: float
) -> tuple[float, float, float]:
    r, g, b = (t * amount + c * (1 - amount) for t, c in zip(top, base, strict=True))
    return (r, g, b)


def pairs() -> list[tuple[str, str, float]]:
    """Every text color on every background it is used on."""
    t = {k: rgb(v) for k, v in tokens().items()}
    out = []
    for fg in NEUTRAL_TEXT + ACCENTS:
        for bg in SURFACES:
            out.append((fg, bg, ratio(t[fg], t[bg])))
    for accent in ACCENTS:  # tinted areas are always mixed over --surface
        tint = mix(t[accent], t["surface"], TINT)
        for fg in [*NEUTRAL_TEXT, accent]:
            out.append((fg, f"{accent} tint", ratio(t[fg], tint)))
    toast = mix(t["coral"], t["surface"], 0.16)  # refusal toast and problem banner
    for fg in NEUTRAL_TEXT:
        out.append((fg, "coral 16% on surface", ratio(t[fg], toast)))
    for accent in ACCENTS:  # badges and primary buttons: --bg text on a solid accent
        out.append(("bg", f"{accent} fill", ratio(t["bg"], t[accent])))
    return out


# ---------------------------------------------------------------- tests


def test_the_token_block_defines_every_role() -> None:
    assert set(tokens()) == {*SURFACES, "border", "line", *NEUTRAL_TEXT, *ACCENTS}


def test_every_text_pair_reaches_4_5_to_1() -> None:
    failing = [(fg, bg, round(r, 2)) for fg, bg, r in pairs() if r < MINIMUM]
    assert not failing, f"below {MINIMUM}:1: {failing}"


def test_the_line_color_is_never_text() -> None:
    assert not re.search(r"(?<![-\w])color:\s*var\(--line\)", CSS), "--line is for connectors only"


def test_no_color_is_named_outside_the_token_block() -> None:
    outside = CSS.replace(token_block(), "")
    color_literal = re.compile(r"#[0-9A-Fa-f]{3,8}\b|\brgba?\(|\bhsla?\(")
    named = re.compile(r":\s*(white|black|gray|grey|red|blue|teal|cyan|aqua|turquoise)\b", re.IGNORECASE)
    for name, text in [("styles.css", outside), ("app.js", JS), ("index.html", HTML)]:
        assert not color_literal.findall(text), f"{name} names a color outside the tokens"
        assert not named.findall(text), f"{name} uses a named color"


def test_there_is_no_teal() -> None:
    for name, value in tokens().items():
        hue, _, saturation = colorsys.rgb_to_hls(*rgb(value))
        if saturation > 0.3 and name in ACCENTS:
            assert not 160 <= hue * 360 <= 195, f"--{name} {value} is teal"
    assert "teal" not in (CSS + JS + HTML).lower()


def test_fonts_are_vendored_and_the_mono_stack_never_falls_back_to_courier() -> None:
    for src in re.findall(r'url\("(fonts/[^"]+)"\)', CSS):
        assert (WEB / src).is_file(), src
    assert (WEB / "fonts" / "Inter-OFL.txt").is_file() and (WEB / "fonts" / "JetBrainsMono-OFL.txt").is_file()
    assert '--mono: "JetBrains Mono", "SFMono-Regular", Menlo, monospace;' in CSS
    assert "courier" not in (CSS + JS + HTML).lower()
    assert "fonts.googleapis" not in HTML and "cdn" not in HTML.lower()


def test_icons_are_vendored_lucide() -> None:
    names = re.findall(r'"([a-z-]+)"', JS.split("const ICONS = [")[1].split("];")[0])
    for name in names:
        svg = WEB / "icons" / f"{name}.svg"
        assert svg.is_file(), name
        assert "lucide" in svg.read_text()
    assert "ISC License" in (WEB / "icons" / "LICENSE.txt").read_text()
    assert "stroke-width: 1.75" in CSS


def test_stable_ids_zoom_targets_and_reduced_motion() -> None:
    source = HTML + JS
    for element_id in STABLE_IDS:
        if element_id.endswith("-"):  # one per tool, rule, stage, node, or Worker, filled in by app.js
            rendered = [f'id="{element_id}${{', f"`{element_id}${{"]
        else:
            rendered = [f'id="{element_id}"', f'"{element_id}"']
        assert any(r in source for r in rendered), element_id
    for target in ZOOM_TARGETS:
        assert f'data-zoom="{target}"' in HTML, target
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert 'params.get("guides") === "1"' in JS


def test_recording_mode_keeps_the_reserved_zones_clear() -> None:
    """Caption band y 960-1080; webcam corner x 1500-1920, y 844-1080."""
    record = {
        sel: dict(re.findall(r"(left|top|width|height):\s*(\d+)px", body))
        for sel, body in re.findall(r"body\.record (#topbar|\.col(?:\.\w+)?) \{([^}]*)\}", CSS)
    }
    for column in (".col.left", ".col.center", ".col.right"):
        top, height = int(record[".col"]["top"]), int(record[column]["height"])
        assert top + height <= 944 if column != ".col.right" else top + height <= 828, column
    right = record[".col.right"]
    assert int(right["left"]) + int(right["width"]) <= 1896
    center = record[".col.center"]
    assert int(center["left"]) + int(center["width"]) <= 1500, (
        "the center column stays left of the webcam corner"
    )


def test_a_blocked_menu_item_stays_readable_and_does_not_highlight() -> None:
    """Full-strength text, a lock and a short reason, no hover highlight; the tooltip has the sentence."""
    assert ".menu button.blocked { color: var(--text);" in CSS
    assert ".menu button.blocked:hover { background: transparent; }" in CSS
    assert 'icon(blocked ? "lock"' in JS and 'class="why">· ' in JS and 'role="tooltip"' in JS


def test_status_badges_sit_away_from_the_endpoint_labels() -> None:
    """Endpoint labels are drawn between the gateway and the teams, so the gateway's
    badge sits on its left edge and the team nodes' badges on their right edge."""
    wait = re.search(r"\.node \.badge-wait \{([^}]*)\}", CSS)
    busy = re.search(r"\.node \.badge-busy \{([^}]*)\}", CSS)
    assert wait and "left: -14px" in wait.group(1) and "right:" not in wait.group(1)
    assert busy and "right: -14px" in busy.group(1) and "left:" not in busy.group(1)


def main() -> None:
    print(f"{'text':<11} {'background':<26} ratio")
    for fg, bg, r in pairs():
        print(f"--{fg:<9} {bg:<26} {r:5.2f}:1 {'ok' if r >= MINIMUM else 'FAIL'}")
    worst = min(pairs(), key=lambda p: p[2])
    print(
        f"\n{len(pairs())} pairs; lowest {worst[2]:.2f}:1 (--{worst[0]} on {worst[1]}); minimum {MINIMUM}:1"
    )


if __name__ == "__main__":
    main()
