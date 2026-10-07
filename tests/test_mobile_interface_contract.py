from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_observer_is_permanent_read_only_surface():
    mobile = (ROOT / "interface" / "mobile.html").read_text(encoding="utf-8")
    script = (ROOT / "interface" / "mobile.js").read_text(encoding="utf-8")
    css = (ROOT / "interface" / "mobile.css").read_text(encoding="utf-8")
    manifest = (ROOT / "interface" / "mobile.webmanifest").read_text(encoding="utf-8")
    desktop = (ROOT / "interface" / "index.html").read_text(encoding="utf-8")
    server = (ROOT / "scripts" / "serve_readonly_interface.py").read_text(encoding="utf-8")

    assert 'viewport-fit=cover' in mobile
    assert 'READ ONLY' in mobile
    assert 'PAPER / SHADOW' in mobile
    assert 'href="/mobile.webmanifest"' in mobile
    assert 'href="/mobile.html"' in desktop
    assert "fetch('/api/dashboard'" in script
    assert "setInterval" in script
    assert "visibilitychange" in script
    assert '@media(min-width:600px)' in css
    assert 'env(safe-area-inset-bottom)' in css
    assert '"start_url": "/mobile.html"' in manifest
    assert '"display": "standalone"' in manifest

    # The mobile observer must never create a write path.
    assert 'method:' not in script
    assert 'do_POST = _reject_mutation' in server
    assert 'do_PUT = _reject_mutation' in server
    assert 'do_PATCH = _reject_mutation' in server
    assert 'do_DELETE = _reject_mutation' in server
    assert 'parser.add_argument("--host", default="127.0.0.1")' in server


def test_full_terminal_keeps_mobile_breakpoints():
    desktop = (ROOT / "interface" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "interface" / "styles.css").read_text(encoding="utf-8")
    assert 'name="viewport"' in desktop
    assert '@media(max-width:540px)' in styles
    assert '@media(max-width:700px)' in styles
