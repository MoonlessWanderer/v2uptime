"""English copy: untouched defaults migrate, and bot, CLI, and source stay Latin."""

import re
import subprocess
import sys
from pathlib import Path

from app.db import _migrate_legacy_defaults
from app.legacy_defaults import LEGACY_DEFAULTS
from app.models import Setting
from app.services.telegram import messages
from app.timeutil import now_ts

ARABIC = re.compile(r"[\u0600-\u06FF]")
ROOT = Path(__file__).resolve().parents[1]
SKIP_NAMES = {"legacy_defaults.py"}
SKIP_SUFFIXES = {".woff", ".woff2", ".ttf", ".otf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}


def _assert_latin(label: str, text: str) -> None:
    match = ARABIC.search(text)
    assert match is None, f"{label} contains U+{ord(match.group()):04X}"


def test_legacy_defaults_replace_only_exact_matches(database):
    old = {key: previous for key, previous, _new in LEGACY_DEFAULTS}
    new = {key: current for key, _previous, current in LEGACY_DEFAULTS}
    for key, previous in old.items():
        Setting.create(key=key, value=previous)

    _migrate_legacy_defaults()
    stored = {row.key: row.value for row in Setting.select()}
    assert stored["site_title"] == new["site_title"]
    assert stored["site_description"] == new["site_description"]
    assert stored["test_location"] == new["test_location"]

    Setting.update(value="My status page").where(Setting.key == "site_title").execute()
    Setting.update(value=old["site_description"]).where(Setting.key == "site_description").execute()
    _migrate_legacy_defaults()
    _migrate_legacy_defaults()
    stored = {row.key: row.value for row in Setting.select()}
    assert stored["site_title"] == "My status page"
    assert stored["site_description"] == new["site_description"]
    assert stored["test_location"] == new["test_location"]


def test_telegram_messages_are_english(database):
    at = now_ts()
    name = "Germany"
    outputs = [
        messages.server_down(name, at, "timeout", 99.5),
        messages.server_up(name, at - 600, at, 600),
        messages.flapping(name, 5, 30, at),
        messages.flap_end(name, True, at),
        messages.network_down(at, 4),
        messages.network_up(at - 600, at),
        messages.xray_crash(at, 1, "exit"),
        messages.xray_restarted(at, "25.1.1"),
        messages.panel_started(at, "1.0.1", 3),
        messages.test_message(at, "direct"),
        messages.delay_note(at - 180, at),
        messages.help_text(),
        messages.merged_header(12),
    ]
    row = messages.ReportRow(name, 99.5, 1, 120, 80)
    outputs.extend(messages.report("Daily report", "24h", [row], (name, 80), (name, 97.0), at))
    outputs.extend(messages.report("Weekly report", "7d", [row], None, None, at))
    outputs.extend(messages.status_overview([(name, "online", 80, 99.5)], at))
    outputs.extend(messages.incidents_overview([(name, at - 120, None, "timeout")], at))
    assert any("late" in text for text in outputs)
    assert any("Fastest" in text for text in outputs)
    for index, text in enumerate(outputs):
        _assert_latin(f"telegram[{index}]", text)


def test_cli_help_is_english():
    proc = subprocess.run(
        [sys.executable, "main.py", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0
    assert "create-admin" in proc.stdout
    _assert_latin("cli --help", proc.stdout + proc.stderr)


def test_source_tree_has_no_arabic_script():
    paths = [ROOT / "install.sh", ROOT / "README.md"]
    paths.extend(path for path in (ROOT / "app").rglob("*") if path.is_file())
    hits: list[str] = []
    for path in paths:
        if path.name in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = ARABIC.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(ROOT)}:{line}")
    assert hits == []
