"""Static contract tests for the served pages.

These encode measurements taken in a real browser at 1440x900. They are not a
substitute for looking at the interface, but they stop the three regressions
that made it look unfinished: type sizes drifting off any scale, prose lines
running past a readable measure, and emoji-only controls with no name for a
screen reader.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = sorted(p for p in WEB.glob("*.html") if p.name != "dynamic_video_generator_app.html")

#: The type scale, in px. Nine sizes drifted between 9px and 13px before this
#: existed; the floor is 10px because uppercase micro-labels below that stop
#: being legible on a laptop panel.
SCALE = {"10": "2xs", "11": "xs", "12": "sm", "13": "md", "15": "lg", "18": "xl", "22": "2xl"}

#: 1ch is the advance width of "0", ~1.31x the average glyph in this font
#: stack: 78ch measured 102 characters. 56ch keeps prose inside the 65-75
#: characters that WCAG-adjacent readability guidance asks for.
MAX_PROSE_CH = 56

_FONT_SIZE = re.compile(r"font-size:\s*([0-9.]+)px")
_MAX_CH = re.compile(r"max-width:\s*([0-9.]+)ch")
_BUTTON = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff←-➿⬀-⯿️ -‍]+"
)


def _src(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


NAMES = [p.name for p in PAGES]


@pytest.mark.parametrize("name", NAMES)
def test_every_font_size_comes_from_the_scale(name):
    """No raw px font sizes: each one must name a step of the shared scale."""
    src = _src(name)
    literals = sorted({v for v in _FONT_SIZE.findall(src)} - set(SCALE))
    assert not literals, f"{name}: off-scale font sizes {literals}"


@pytest.mark.parametrize("name", NAMES)
def test_the_scale_tokens_are_defined(name):
    """A page that uses var(--fs-*) has to define the whole scale itself."""
    src = _src(name)
    if "var(--fs-" not in src:
        pytest.skip(f"{name} declares no font sizes")
    missing = [s for s in SCALE.values() if f"--fs-{s}:" not in src]
    assert not missing, f"{name}: undefined scale steps {missing}"


@pytest.mark.parametrize("name", NAMES)
def test_prose_stays_inside_a_readable_measure(name):
    """Line length: 65-75 characters, expressed in this font's ch units."""
    src = _src(name)
    wide = [c for c in _MAX_CH.findall(src) if float(c) > MAX_PROSE_CH]
    assert not wide, f"{name}: prose measure {wide}ch exceeds {MAX_PROSE_CH}ch"


@pytest.mark.parametrize("name", NAMES)
def test_emoji_only_controls_carry_an_accessible_name(name):
    """A button whose whole label is an emoji needs title or aria-label."""
    src = _src(name)
    unnamed = []
    for attrs, inner in _BUTTON.findall(src):
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if not text or not _EMOJI.search(text):
            continue
        if _EMOJI.sub("", text).strip():
            continue  # emoji sits next to real words
        if "title=" in attrs or "aria-label=" in attrs:
            continue
        unnamed.append(text[:12])
    assert not unnamed, f"{name}: unnamed emoji-only buttons {unnamed}"


@pytest.mark.parametrize("name", NAMES)
def test_pages_declare_english_as_their_language(name):
    """lang= drives screen-reader pronunciation; the pages are English now."""
    m = re.search(r"<html[^>]*\blang=\"([^\"]+)\"", _src(name))
    assert m, f"{name}: no lang attribute on <html>"
    assert m.group(1).startswith("en"), f"{name}: lang=\"{m.group(1)}\""


@pytest.mark.parametrize("name", NAMES)
def test_pages_declare_the_pointer_target_floor(name):
    """WCAG 2.2 AA (2.5.8): no pointer target smaller than 24x24 CSS px.

    Measured before the rule existed: railToggle 24x22, the volume slider
    54x16, the drawer close buttons 23x20, and the Top-10 links 172x14.
    """
    src = _src(name)
    if "<button" not in src:
        pytest.skip(f"{name} has no controls")
    assert "--target-min" in src, f"{name}: no pointer-target floor declared"


#: Words that are French and cannot legitimately appear in an English UI.
#: "selection" is deliberately absent: ``::selection`` is a CSS pseudo-element.
_FRENCH = re.compile(
    r"\b(chargement|moniteur|largeur|hauteur|suivant|pr[ée]c[ée]dent|en cours"
    r"|file d'attente|en attente|au repos|aucune?|veuillez|param[èe]tres"
    r"|r[ée]glages|t[ée]l[ée]charg\w+|enregistrer|supprimer|annuler"
    r"|trame|panneaux?|globale|voix|rapatrier)\b",
    re.I,
)


#: A hand-picked word list only catches the words someone thought of: it passed
#: green over "injoignable", "obligatoire", "raccourcis", "Lire l'image" and
#: "S'abonner". Grammar generalises where vocabulary does not -- an elided
#: article and a handful of function words cannot occur in English prose.
_FRENCH_GRAMMAR = re.compile(
    r"\b[ldjnmtsc]'[a-zéèêàçûôîù]{2,}"
    r"|\b(les|des|une|pour|avec|dans|sont|cette|leur|nous|vous|tout|tous"
    r"|aussi|donc|mais|puis|alors|encore|toujours|jamais|depuis|pendant"
    r"|chaque|plusieurs|aucun|silencieux|injoignable|indisponible"
    r"|obligatoire|raccourcis?|fichiers?)\b",
    re.I,
)
#: Some French is content, not chrome: the Prompting tutor *detects* French
#: input, and the mock produces French dialogue on request. A line carrying
#: this marker is data, not user-facing copy.
_I18N_EXEMPT = "i18n-exempt"


@pytest.mark.parametrize("name", NAMES)
def test_no_french_left_in_the_pages(name):
    """The repository is English; French strings shipped to users are bugs."""
    hits = sorted({m.group(0).lower() for m in _FRENCH.finditer(_src(name))})
    assert not hits, f"{name}: French strings {hits}"


@pytest.mark.parametrize("name", NAMES)
def test_no_french_grammar_left_in_the_pages(name):
    """Same rule, caught by structure rather than by a vocabulary list."""
    hits = sorted(
        {
            m.group(0).lower()
            for line in _src(name).splitlines()
            if _I18N_EXEMPT not in line
            for m in _FRENCH_GRAMMAR.finditer(line)
        }
    )
    assert not hits, f"{name}: French strings {hits}"


@pytest.mark.parametrize("name", NAMES)
def test_focus_ring_is_declared_after_any_outline_suppression(name):
    """WCAG 2.4.7. index.html already had the ring, at line 54 -- and six
    later ``outline:none`` rules at equal specificity silently beat it, so
    every focused control computed outline-style:none. Source order decides.
    """
    src = _src(name)
    if "<button" not in src:
        pytest.skip(f"{name} has no controls")
    ring = src.rfind(":focus-visible")
    assert ring != -1, f"{name}: no :focus-visible rule"
    suppressed = max(src.rfind("outline:none"), src.rfind("outline: none"))
    assert ring > suppressed, f"{name}: focus ring at {ring} loses to outline:none at {suppressed}"


#: Colour emoji. Excludes the monochrome dingbats the UI uses as text
#: (checks, crosses, warning sign) — those inherit currentColor and theme
#: correctly, unlike a colour glyph baked by the platform font.
_COLOUR_EMOJI = re.compile("[\U0001F300-\U0001FAFF✨⚡⚙⚖❤⚛]")

_CONTROL = re.compile(
    r"<(button|summary|h1|h2|h3)\b[^>]*>(.*?)</\1>|<strong>(.*?)</strong>", re.S
)


@pytest.mark.parametrize("name", NAMES)
def test_controls_use_svg_icons_not_emoji(name):
    """Buttons, disclosure summaries and headings carry drawn icons.

    Emoji are baked by the platform font: they ignore the theme, shift
    metrics between machines, and cannot take a stroke weight. Reaction
    pickers and the favicon are content and stay emoji.
    """
    hits = []
    for m in _CONTROL.finditer(_src(name)):
        inner = m.group(2) or m.group(3) or ""
        text = re.sub(r"<svg.*?</svg>", "", inner, flags=re.S)
        for e in _COLOUR_EMOJI.findall(text):
            hits.append(e)
    assert not hits, f"{name}: emoji in controls {sorted(set(hits))}"


#: Spacing rhythm: multiples of 4px, with a 2px half-step kept for hairline
#: offsets. Before this there were 34 distinct values and 569 of 878
#: declarations sat off any grid (10, 6, 14, 9, 7, 18, 11, 3, 5, 13, 15, 22...).
def _on_rhythm(px: float) -> bool:
    return px == 0 or abs(px) == 2 or abs(px) % 4 == 0

_SPACING_DECL = re.compile(
    r"\b(?:padding|margin|gap|row-gap|column-gap)"
    r"(?:-(?:top|right|bottom|left|inline|block))?:\s*([^;\"}\)]+)"
)


@pytest.mark.parametrize("name", NAMES)
def test_spacing_sits_on_the_rhythm(name):
    """Padding, margin and gap come from one scale, not from 34 loose values."""
    off = set()
    for m in _SPACING_DECL.finditer(_src(name)):
        for tok in re.findall(r"(-?\d+(?:\.\d+)?)px", m.group(1)):
            if not _on_rhythm(float(tok)):
                off.add(tok)
    assert not off, f"{name}: off-rhythm spacing {sorted(off, key=float)}"


@pytest.mark.parametrize("name", NAMES)
def test_no_backend_identifiers_are_committed(name):
    """The README promises the repository carries no infrastructure ids.

    The Supabase project ref and its publishable key were hardcoded in
    index.html, so every clone of a public repo pointed its community client
    at one private project.
    """
    src = _src(name)
    leaks = re.findall(
        r"https://[a-z0-9]{15,}\.supabase\.co"
        r"|sb_publishable_[A-Za-z0-9_-]+"
        r"|https://[a-z0-9-]+\.firebaseio\.com"
        r"|eyJ[A-Za-z0-9_-]{30,}",
        src,
    )
    assert not leaks, f"{name}: committed identifiers {sorted(set(leaks))}"


@pytest.mark.parametrize("name", NAMES)
def test_controls_declare_press_and_disabled_states(name):
    """A control that looks pressable owes the user pressed and disabled.

    Audited before this: .btn had no :disabled on three pages and no :active
    on four, .load / .iconbtn / .chip / .minibtn / .plrow had neither, and
    ltx_studio's .btn had no :hover either.
    """
    src = _src(name)
    if "<button" not in src:
        pytest.skip(f"{name} has no controls")
    for state in (":active", ":disabled", "aria-busy"):
        assert state in src, f"{name}: no {state} rule"


def test_no_backend_identifiers_in_the_docs():
    """The same rule as the pages, for everything a reader is pointed at.

    docs/COMMUNITY_SPEC.md named a live Supabase project by its ref, in a
    paragraph about which project *not* to reuse.
    """
    root = WEB.parent
    for path in list((root / "docs").rglob("*.md")) + [root / "README.md"]:
        text = path.read_text(encoding="utf-8")
        leaks = re.findall(
            r"https://[a-z0-9]{15,}\.supabase\.co"
            r"|sb_publishable_[A-Za-z0-9_-]+"
            r"|`[a-z]{20}`"
            r"|eyJ[A-Za-z0-9_-]{30,}",
            text,
        )
        assert not leaks, f"{path.name}: committed identifiers {sorted(set(leaks))}"


#: A lookup dereferenced on the spot — `$('x').foo` — throws when the id is
#: absent. `const el = $('x'); if (el) ...` is fine and stays out of this.
_DOM_ID = re.compile(
    r"""(?:\$|getElementById)\(['"]([A-Za-z][\w-]*)['"]\)\s*\."""
)


@pytest.mark.parametrize("name", NAMES)
def test_scripts_only_reach_for_ids_the_page_has(name):
    """A missing id throws, and everything after it in that script stops.

    Moving the guide out of the shell left `window.Doc=Doc` behind: it threw on
    load and killed the three lines after it, one of which wired the rail
    collapse button. `node --check` cannot see this — it is a runtime error in
    syntactically valid code.
    """
    src = _src(name)
    present = set(re.findall(r"""\bid=["']([^"']+)["']""", src))
    # ids created at runtime, and the ones another page owns
    present |= {"toasts"}
    missing = set()
    for m in _DOM_ID.finditer(src):
        if m.group(1) not in present:
            missing.add(m.group(1))
    assert not missing, f"{name}: unguarded reach for absent ids {sorted(missing)}"


@pytest.mark.parametrize("name", NAMES)
def test_exports_name_something_the_page_defines(name):
    """`window.X = X` for an X that left throws and stops the script.

    index.html kept exporting the guide's module after the guide moved to its
    own page. The ReferenceError killed the three lines after it, one of which
    wired the rail-collapse button — so the rail simply stopped collapsing,
    with nothing in the syntax to show for it.
    """
    src = _src(name)
    defined = set(re.findall(r"\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", src))
    orphans = {
        m.group(1)
        for m in re.finditer(r"window\.([A-Za-z_$][\w$]*)\s*=\s*\1\b", src)
        if m.group(1) not in defined
    }
    assert not orphans, f"{name}: exports undefined names {sorted(orphans)}"



#: A background literal dark enough that near-black ink disappears on it.
_DARK_BG = re.compile(
    r"background(?:-color|-image)?\s*:\s*([^;{}]+)", re.I
)
_HEX = re.compile(r"#([0-9a-f]{3}|[0-9a-f]{6})\b", re.I)
_RGBA = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.]+))?\s*\)")
_REPLACED = re.compile(r"\b(video|img|canvas|iframe)\s*$", re.I)


def _luma(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def _has_dark_literal(value: str) -> bool:
    """True when the declaration paints a ground too dark for near-black ink."""
    for m in _HEX.finditer(value):
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if _luma(*(int(h[i : i + 2], 16) for i in (0, 2, 4))) < 80:
            return True
    for m in _RGBA.finditer(value):
        r, g, b = (float(m.group(i)) for i in (1, 2, 3))
        alpha = float(m.group(4)) if m.group(4) else 1.0
        if alpha > 0.5 and _luma(r, g, b) < 80:
            return True
    return False


def _rules(src: str):
    """(selector, declarations) for every rule inside a <style> block."""
    for style in re.findall(r"<style[^>]*>(.*?)</style>", src, re.S | re.I):
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
            yield sel.strip(), body


def dark_grounds_without_ink(src: str) -> list[str]:
    """Rules that paint a permanently dark ground but inherit their ink.

    ``--fg`` flips to near-black on the light theme. A scrim over a video, a
    lightbox arrow or a side drawer that keeps its dark ground in both themes
    therefore has to name its own colour, or it paints black on black.
    """
    bad = []
    for sel, body in _rules(src):
        if sel.startswith("@") or "--" in sel:
            continue
        # <video>, <img> and <canvas> are replaced elements: they letterbox on
        # black and never paint a text child, so ink cannot apply to them.
        if all(
            _REPLACED.search(part) for part in sel.split(",") if part.strip()
        ):
            continue
        decls = [d for d in body.split(";") if d.strip()]
        if not any(
            _has_dark_literal(m.group(1))
            for d in decls
            for m in [_DARK_BG.match(d.strip())]
            if m
        ):
            continue
        if not any(re.match(r"\s*color\s*:", d) for d in decls):
            bad.append(sel)
    return bad


@pytest.mark.parametrize("name", NAMES)
def test_permanently_dark_grounds_name_their_own_ink(name):
    """Measured on the light page before the fix: the hover controls on a
    gallery tile computed 1.04:1 -- near-black ink on a near-black scrim.
    """
    bad = dark_grounds_without_ink(_src(name))
    assert not bad, f"{name}: dark ground with inherited ink -> {bad}"


#: Everything shipped in the repository, not only the pages: the example pack's
#: labels and status strings reach the UI through /transforms.
_SHIPPED = ("packs", "scripts", "docs", "src")


def _shipped_files():
    root = Path(__file__).resolve().parent.parent
    for folder in _SHIPPED:
        for path in sorted((root / folder).rglob("*")):
            if path.suffix in {".py", ".json", ".md", ".sh", ".txt"} and path.is_file():
                yield path


def test_no_french_left_anywhere_shipped():
    """The example pack shipped "Restylage en film noir" and "Rue, nuit"."""
    bad = {}
    for path in _shipped_files():
        hits = sorted(
            {
                m.group(0).lower()
                for line in path.read_text(encoding="utf-8").splitlines()
                if _I18N_EXEMPT not in line
                for m in _FRENCH_GRAMMAR.finditer(line)
            }
        )
        if hits:
            bad[str(path.name)] = hits
    assert not bad, f"French outside the pages: {bad}"
