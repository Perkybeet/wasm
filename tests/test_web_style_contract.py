# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the panel's stylesheet and shell, as contracts rather than as taste.

Nothing here is a style opinion. Each test pins a property the panel was
shipped without, where the absence was invisible to every other test in the
suite because it lives in a ``.css`` file or in the order of two ``<script>``
tags:

- **Something owns the scroll.** ``.shell`` was ``min-height: 100dvh`` with no
  overflow anywhere, so the document was the only scroll container and it took
  the machine strip and the navigation with it on every page.
- **Nothing in the markup needs a policy the panel does not grant.** The shell
  shipped with Alpine, which compiles every ``x-data``, ``@click`` and
  ``x-text`` from a string - forbidden by ``script-src 'self'``. The framework
  never ran once: the log drawer never initialised, the mobile navigation could
  not be opened, and no test could see any of it, because the markup was valid
  and the server was correct. Only a browser enforces a CSP.
- **Every control hook the markup renders is handled by the client**, and the
  reverse. With no framework binding the two, a hook one side knows about and
  the other does not is a dead control that reports nothing.
- **Text meets AA against every surface it is placed on.** ``--text-faint``
  was 2.68:1 and carries certbot's validity lines, backup descriptions and the
  units in the machine strip. It is content, not decoration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "wasm" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
BASE = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
LOGIN = (WEB / "templates" / "login.html").read_text(encoding="utf-8")

TEMPLATES = {
    path.relative_to(WEB / "templates").as_posix(): path.read_text(encoding="utf-8")
    for path in sorted((WEB / "templates").rglob("*.html"))
}
ALL_MARKUP = "\n".join(TEMPLATES.values())

#: WCAG AA for body text.
AA = 4.5


def rule(selector: str) -> str:
    """
    Return the declarations of the first top-level rule for a selector.

    Args:
        selector: The selector as it is written in the stylesheet.

    Returns:
        The declaration block, or an empty string when there is no such rule.
        Only rules at the top level are considered, so a declaration inside a
        media query cannot satisfy a test about the default layout.
    """
    # Anchored to the start of a line, so an indented copy inside a media query
    # cannot satisfy a test about the default layout.
    match = re.search(rf"^{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS, re.MULTILINE)
    return match.group(1) if match else ""


def variables(block: str) -> dict[str, str]:
    """
    Read the custom properties out of a declaration block.

    Args:
        block: A CSS declaration block.

    Returns:
        Property name without the leading dashes, mapped to its value.
    """
    return {name: value.strip() for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", block)}


def theme(name: str) -> dict[str, str]:
    """
    Read the custom properties of one theme.

    Args:
        name: "light" for ``:root``, "dark" for the explicit dark block.

    Returns:
        The theme's custom properties.
    """
    if name == "light":
        match = re.search(r":root\s*\{(.*?)\n\}", CSS, re.DOTALL)
    else:
        match = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\n\}', CSS, re.DOTALL)
    assert match, f"the {name} theme block was not found"
    return variables(match.group(1))


def luminance(colour: str) -> float:
    """
    Relative luminance of a hex colour, per WCAG.

    Args:
        colour: A ``#rrggbb`` string.

    Returns:
        Relative luminance between 0 and 1.
    """
    digits = colour.lstrip("#")
    channels = [int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    """
    Contrast ratio between two colours.

    Args:
        foreground: A ``#rrggbb`` string.
        background: A ``#rrggbb`` string.

    Returns:
        The ratio, between 1 and 21.
    """
    first, second = luminance(foreground), luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Who owns the scroll
# ---------------------------------------------------------------------------


def test_the_machine_strip_stays_put() -> None:
    """It says which machine you are about to change; it cannot scroll away."""
    declarations = rule(".machine")

    assert "position: sticky" in declarations
    assert "top: 0" in declarations


def test_the_sidebar_stays_put() -> None:
    """The navigation scrolled off the top of every page longer than a screen."""
    declarations = rule(".sidebar")

    assert "position: sticky" in declarations
    assert "top: var(--machine-strip-height)" in declarations


def test_the_sidebar_can_be_stuck_to_anything() -> None:
    """
    A stretched grid item has no room to move inside its containing block, so
    sticky silently does nothing. This is the declaration that makes the rest
    of it work, and it looks removable to anyone who does not know that.
    """
    assert "align-self: start" in rule(".sidebar")


def test_a_sidebar_taller_than_the_screen_can_still_be_read() -> None:
    """Pinning it to the viewport clips it unless it scrolls on its own."""
    declarations = rule(".sidebar")

    assert "overflow-y: auto" in declarations
    assert "height: calc(100dvh - var(--machine-strip-height))" in declarations


def test_the_pinned_strip_stays_above_the_content_that_scrolls_under_it() -> None:
    """
    Without a stacking order the page slides over the strip rather than under
    it, which looks like a rendering fault and hides the machine's state.
    """
    assert "z-index" in rule(".machine")


# ---------------------------------------------------------------------------
# The content security policy the panel serves itself under
# ---------------------------------------------------------------------------

#: Everything the shell must not contain. Most of it is refused outright by the
#: panel's Content Security Policy, which sets script-src 'self' with no
#: unsafe-eval because the panel executes systemd as root and an injected
#: script here is a root shell.
#:
#: Style attributes are the exception: style-src does allow inline, because
#: xterm and htmx cannot work without it. They are still banned from templates,
#: for a different reason - the stylesheet is the one place styling lives, and
#: a margin buried in an attribute is a margin nobody finds again.
#:
#: This is the test that was missing. The panel shipped with Alpine, whose
#: every x-data, @click and x-text is a string the browser must compile into a
#: function - which that policy forbids outright. The framework never ran once:
#: the log drawer never initialised, the mobile navigation could not be opened,
#: and every capacity bar rendered empty because style="width: N%" was blocked
#: too. Nothing in the suite could see it, because the markup was valid and the
#: server was correct; only a browser enforces a CSP.
FORBIDDEN_BY_CSP = (
    (r'\sstyle="', "styling belongs in app.css, not in a template attribute"),
    (r"\son[a-z]+=\"", "inline event handlers are blocked by script-src 'self'"),
    (r'href="javascript:', "javascript: URLs are blocked by script-src 'self'"),
    (r"\sx-(data|show|text|html|model|init|bind)=", "Alpine expressions require unsafe-eval"),
    (r'\s@[a-z]+(\.[a-z]+)*="', "Alpine event bindings require unsafe-eval"),
    (r'\s:[a-z-]+="', "Alpine attribute bindings require unsafe-eval"),
)


@pytest.mark.parametrize(("pattern", "reason"), FORBIDDEN_BY_CSP)
def test_no_template_contains_anything_the_panel_s_own_policy_blocks(
    pattern: str, reason: str
) -> None:
    """
    Args:
        pattern: What to look for.
        reason: Why the browser would refuse it.
    """
    offenders = []
    for name, source in TEMPLATES.items():
        # Jinja comments explain these rules and quote them; only real markup
        # counts, so comment blocks are stripped before looking.
        markup = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)
        for number, line in enumerate(markup.splitlines(), 1):
            if re.search(pattern, line):
                offenders.append(f"{name}:{number}: {line.strip()[:80]}")

    assert not offenders, reason + "\n" + "\n".join(offenders)


def test_the_policy_sweep_reads_the_templates_it_claims_to() -> None:
    """A net that reads nothing would pass every check above."""
    assert len(TEMPLATES) >= 10, f"only found {len(TEMPLATES)} templates"
    assert any("drawer__bar" in source for source in TEMPLATES.values())


def test_the_shell_loads_no_script_that_needs_unsafe_eval() -> None:
    """
    Alpine was removed rather than the policy loosened. Vendoring it again
    would put the drawer and the navigation straight back to inert.
    """
    scripts = re.findall(r"<script[^>]*src=\"([^\"]+)\"", BASE)

    assert not any("alpine" in src.lower() for src in scripts), scripts
    assert not (WEB / "static" / "vendor" / "alpine.min.js").exists()


# ---------------------------------------------------------------------------
# The order the client is assembled in
# ---------------------------------------------------------------------------


def test_the_client_script_is_not_a_module() -> None:
    """
    A module is always deferred to the end of the queue. The shell is ordered
    deliberately, so its one script stays classic and ordered with it.
    """
    tag = re.search(r"<script[^>]*/static/panel\.js[^>]*>", BASE)
    assert tag, "the panel script tag was not found"
    assert 'type="module"' not in tag.group(0)


def test_the_client_script_is_deferred() -> None:
    """Without defer it runs before the body exists and binds no listeners."""
    tag = re.search(r"<script[^>]*/static/panel\.js[^>]*>", BASE)
    assert tag and "defer" in tag.group(0)


# ---------------------------------------------------------------------------
# Controls the client has to be able to find
# ---------------------------------------------------------------------------

#: Every data attribute the markup uses as a hook, and what it drives. With no
#: framework binding them, a hook the script does not handle is a dead control
#: and nothing says so.
HOOKS = (
    "data-drawer-toggle",
    "data-drawer-clear",
    "data-drawer-source",
    "data-nav-toggle",
    "data-nav-close",
    "data-theme-toggle",
    "data-follow-log",
    "data-metric",
    "data-chart",
    "data-chart-window",
)


@pytest.mark.parametrize("hook", HOOKS)
def test_every_control_hook_in_the_markup_is_handled_by_the_client(hook: str) -> None:
    """
    Args:
        hook: A data attribute the shell renders.
    """
    script = (WEB / "static" / "panel.js").read_text(encoding="utf-8")

    assert hook in ALL_MARKUP, f"{hook} is handled but never rendered"
    assert hook in script, f"{hook} is rendered but nothing handles it"


# ---------------------------------------------------------------------------
# Contrast
# ---------------------------------------------------------------------------

#: The surfaces text is actually placed on, per theme.
BACKGROUNDS = ("bg", "bg-sunken", "surface", "surface-hover")

#: The text colours that carry content rather than decoration. --text-faint is
#: on certbot's validity line, a backup's description and the machine strip's
#: units, so "it is only a hint" was never true of it.
FOREGROUNDS = ("text", "text-muted", "text-faint")


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("foreground", FOREGROUNDS)
@pytest.mark.parametrize("background", BACKGROUNDS)
def test_text_meets_aa_on_every_surface_it_is_placed_on(
    mode: str, foreground: str, background: str
) -> None:
    """
    Args:
        mode: Which theme.
        foreground: The text colour token.
        background: The surface token.
    """
    tokens = theme(mode)
    ratio = contrast(tokens[foreground], tokens[background])

    assert ratio >= AA, (
        f"{mode}: --{foreground} ({tokens[foreground]}) on --{background} "
        f"({tokens[background]}) is {ratio:.2f}:1, below AA at {AA}:1"
    )


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_two_quiet_text_colours_stay_distinguishable(mode: str) -> None:
    """
    Meeting AA by collapsing the palette into one grey would pass the test
    above and lose the hierarchy it exists to keep readable.

    Args:
        mode: Which theme.
    """
    tokens = theme(mode)
    muted = luminance(tokens["text-muted"])
    faint = luminance(tokens["text-faint"])

    assert abs(muted - faint) > 0.01, "--text-muted and --text-faint are the same colour"
    if mode == "light":
        assert muted < faint, "faint must be lighter than muted in the light theme"
    else:
        assert muted > faint, "faint must be darker than muted in the dark theme"


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("state", ["state-active", "state-failed", "state-busy"])
def test_every_state_colour_is_legible_on_its_own_background(mode: str, state: str) -> None:
    """
    A badge is a state colour on a tinted background of the same hue, and the
    design floor asks for AA on the state colours too.

    Args:
        mode: Which theme.
        state: The state token.
    """
    tokens = theme(mode)
    ratio = contrast(tokens[state], tokens[f"{state}-bg"])

    assert ratio >= AA, f"{mode}: --{state} on --{state}-bg is {ratio:.2f}:1"


# ---------------------------------------------------------------------------
# The stylesheet is where the styling lives
# ---------------------------------------------------------------------------


def test_the_sign_in_screen_carries_no_inline_styles() -> None:
    """
    It was laid out by six style attributes, in a stylesheet whose header
    promises that what you read there is what the server sends.
    """
    assert "style=" not in LOGIN


def test_the_sign_in_screen_sits_on_a_surface() -> None:
    """
    It was the only screen in the panel whose content sat on the bare
    background, which is most of why it read as unfinished.
    """
    assert 'class="auth__panel"' in LOGIN
    declarations = rule(".auth__panel")
    assert "background: var(--surface)" in declarations
    assert "border: 1px solid var(--border)" in declarations


def test_the_shell_has_a_real_sidebar_footer() -> None:
    """Sign-out was a bare div with two inline styles and no separator."""
    assert 'class="sidebar__footer"' in BASE
    assert "border-top: 1px solid var(--border)" in rule(".sidebar__footer")


def test_the_charts_are_sized_by_the_stylesheet() -> None:
    """
    uPlot draws into whatever box it is given, and the panel's own style-src
    policy throws away style attributes, so the box has to come from here: a
    chart container without a height from the stylesheet renders zero pixels
    tall and no other test can see it.
    """
    declarations = rule(".chart__plot")

    assert "height:" in declarations, "the chart containers have no fixed height"
    assert "font-variant-numeric: tabular-nums" in declarations, (
        "chart figures must be tabular so the axes do not jitter as they update"
    )


def test_the_notices_stack_clears_the_open_drawer() -> None:
    """
    The stylesheet offsets the toasts by --log-drawer-offset and nothing ever
    set it, so a notice painted on top of the open log drawer.
    """
    assert "--log-drawer-offset" in CSS, "the offset is no longer used"
    panel_js = (WEB / "static" / "panel.js").read_text(encoding="utf-8")
    assert "--log-drawer-offset" in panel_js, "nothing sets the offset the stylesheet reads"
