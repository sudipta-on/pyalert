"""
pyalert.config
==============

Configuration loading/saving, the interactive `pyalert-setup` wizard, and
the Google Apps Script (`Code.gs`) generator.

This module has ZERO imports from `pyalert.notifier` or `pyalert.monitor` on purpose 
— it must be importable standalone by the `pyalert-setup` CLI entry point without pulling 
in psutil, ctypes/NVML bindings, or the notification engine. This keeps the dependency graph acyclic:

    config.py  <-- monitor.py <-- notifier.py <-- __init__.py

`config.py` sits at the bottom and nothing imports *from* it except its siblings.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

CONFIG_DIR = Path(os.environ.get("PYALERT_CONFIG_DIR", Path.home() / ".config" / "pyalert"))
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_SCRIPT_NAME = "Code.gs"


# --------------------------------------------------------------------------- #
# Config model
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    webhook_url: str = ""
    recipient_email: str = ""
    sender_name: str = "PyAlert"
    shared_secret: str = ""          # Authenticates incoming requests to Code.gs
    default_cooldown_seconds: int = 60
    max_attachment_mb: float = 20.0  # Under Gmail's ~25MB cap
    timeout_seconds: int = 15
    retries: int = 1
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        known_fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def validate(self) -> List[str]:
        problems = []
        if not self.webhook_url:
            problems.append("webhook_url is empty — run `pyalert-setup` first.")
        elif not self.webhook_url.startswith("https://script.google.com/"):
            problems.append(
                "webhook_url does not look like a Google Apps Script "
                "deployment URL (expected it to start with 'https://script.google.com/')."
            )
        if not self.recipient_email or "@" not in self.recipient_email:
            problems.append("recipient_email is missing or invalid.")
        if self.default_cooldown_seconds < 0:
            problems.append("default_cooldown_seconds must be >= 0.")
        if self.max_attachment_mb <= 0:
            problems.append("max_attachment_mb must be > 0.")
        return problems


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #

def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "posix":
            os.chmod(CONFIG_DIR, stat.S_IRWXU)
    except OSError:
        pass


def load_config(path: Optional[Path] = None) -> Config:
    cfg_path = path or CONFIG_FILE
    data: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            sys.stderr.write(
                f"[pyalert] WARNING: could not read config at {cfg_path} ({exc}). "
                "Falling back to defaults/environment variables.\n"
            )
            data = {}

    cfg = Config.from_dict(data)

    cfg.webhook_url = os.environ.get("PYALERT_WEBHOOK_URL", cfg.webhook_url)
    cfg.recipient_email = os.environ.get("PYALERT_RECIPIENT", cfg.recipient_email)
    cfg.shared_secret = os.environ.get("PYALERT_SHARED_SECRET", cfg.shared_secret)
    cfg.sender_name = os.environ.get("PYALERT_SENDER_NAME", cfg.sender_name)
    if os.environ.get("PYALERT_COOLDOWN"):
        try:
            cfg.default_cooldown_seconds = int(os.environ["PYALERT_COOLDOWN"])
        except ValueError:
            pass
    if os.environ.get("PYALERT_DRY_RUN"):
        cfg.dry_run = os.environ["PYALERT_DRY_RUN"].lower() in ("1", "true", "yes")

    return cfg


def save_config(cfg: Config, path: Optional[Path] = None) -> Path:
    cfg_path = path or CONFIG_FILE
    _ensure_config_dir()
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2, sort_keys=True)
    try:
        if os.name == "posix":
            os.chmod(cfg_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return cfg_path


# --------------------------------------------------------------------------- #
# Interactive wizard
# --------------------------------------------------------------------------- #

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            value = getpass.getpass(f"{label}{suffix}: ")
        else:
            value = input(f"{label}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        print("\n[pyalert] Setup cancelled.")
        sys.exit(1)
    return value.strip() or default


def run_wizard(existing: Optional[Config] = None) -> Config:
    print("=" * 62)
    print(" pyalert setup wizard")
    print("=" * 62)
    print(
        "This creates ~/.config/pyalert/config.json with the settings\n"
        "pyalert needs to send email digests through YOUR OWN Google Apps\n"
        "Script bridge. No Gmail password is ever requested or stored.\n"
    )

    cfg = existing or Config()

    print("Step 1/5 — Deploy the bridge script")
    print(
        "  If you haven't already, run:\n"
        "    pyalert-setup --generate-script\n"
        "  then follow the printed instructions to deploy it in Google\n"
        "  Apps Script and copy the resulting Web App URL.\n"
    )
    cfg.webhook_url = _prompt("Apps Script Web App URL", cfg.webhook_url)

    print("\nStep 2/5 — Where should alerts be sent?")
    cfg.recipient_email = _prompt("Recipient email address", cfg.recipient_email)

    print("\nStep 3/5 — Cosmetic sender name shown in the email 'From' line")
    cfg.sender_name = _prompt("Sender display name", cfg.sender_name or "PyAlert")

    print(
        "\nStep 4/5 — Shared secret authentication token.\n"
        "  Must match the secret token embedded in your deployed Code.gs.\n"
    )
    cfg.shared_secret = _prompt("Shared secret token", cfg.shared_secret, secret=True)

    print("\nStep 5/5 — Throttling defaults")
    cooldown_raw = _prompt(
        "Default cooldown between digest emails, in seconds",
        str(cfg.default_cooldown_seconds or 60),
    )
    try:
        cfg.default_cooldown_seconds = max(0, int(cooldown_raw))
    except ValueError:
        cfg.default_cooldown_seconds = 60

    problems = cfg.validate()
    if problems:
        print("\n[pyalert] WARNING: configuration has issues:")
        for p in problems:
            print(f"  - {p}")
        print("You can re-run `pyalert-setup` any time to fix these.\n")

    path = save_config(cfg)
    print(f"\n✅ Saved configuration to {path}")
    return cfg


# --------------------------------------------------------------------------- #
# Apps Script (Code.gs) generator template
# --------------------------------------------------------------------------- #

_APPS_SCRIPT_TEMPLATE = r"""/**
 * pyalert bridge — Google Apps Script Web App
 * ---------------------------------------------
 * Auto-generated by `pyalert-setup --generate-script`.
 *
 * DEPLOYMENT
 * 1. Go to https://script.google.com/ and paste in this entire file.
 * 2. Click Deploy -> New deployment (or Manage deployments -> Edit).
 * 3. Set Version to "New version".
 * 4. Set Execute as: "Me" and Who has access: "Anyone".
 * 5. Click Deploy, authorize permissions, and copy the Web App URL.
 */

var SHARED_SECRET = "__SHARED_SECRET__";

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _jsonResponse({ ok: false, error: "empty payload" }, 400);
    }

    // Explicitly parse incoming payload as UTF-8 so symbols and unicode render cleanly
    var rawString = Utilities.newBlob(e.postData.contents).getDataAsString("UTF-8");
    var payload = JSON.parse(rawString);

    if (SHARED_SECRET && payload.shared_secret !== SHARED_SECRET) {
      return _jsonResponse({ ok: false, error: "unauthorized: invalid shared secret" }, 401);
    }

    var to = payload.recipient_email;
    var subject = payload.subject || "[pyalert] Notification";
    var htmlBody = payload.html_body || "<p>(empty body)</p>";
    var senderName = payload.sender_name || "PyAlert";
    var attachments = _buildAttachments(payload.attachments || []);

    var options = {
      htmlBody: htmlBody,
      name: senderName,
    };
    if (attachments.length > 0) {
      options.attachments = attachments;
    }

    GmailApp.sendEmail(to, subject, _stripHtml(htmlBody), options);

    return _jsonResponse({ ok: true });
  } catch (err) {
    return _jsonResponse({ ok: false, error: String(err) }, 500);
  }
}

function doGet(e) {
  return _jsonResponse({ ok: true, message: "pyalert bridge is active." });
}

function _buildAttachments(rawAttachments) {
  var out = [];
  for (var i = 0; i < rawAttachments.length; i++) {
    var a = rawAttachments[i];
    try {
      var bytes = Utilities.base64Decode(a.content_base64 || a.data);
      var blob = Utilities.newBlob(bytes, a.mime_type || "application/octet-stream", a.filename || ("attachment_" + i));
      out.push(blob);
    } catch (err) {
      continue;
    }
  }
  return out;
}

function _stripHtml(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function _jsonResponse(obj, statusCode) {
  var output = ContentService.createTextOutput(JSON.stringify(obj));
  output.setMimeType(ContentService.MimeType.JSON);
  return output;
}
"""


def generate_apps_script(shared_secret: Optional[str] = None, output_path: Optional[Path] = None) -> Tuple[Path, str]:
    """Render Code.gs with the shared secret baked in and write it to disk."""
    if shared_secret is None:
        print("=" * 62)
        print(" PyAlert Apps Script Generator")
        print("=" * 62)
        entered = input("Enter secret key to embed into Code.gs (leave blank to auto-generate): ").strip()
        actual_secret = entered if entered else secrets.token_hex(16)
    else:
        actual_secret = shared_secret.strip() or secrets.token_hex(16)

    content = _APPS_SCRIPT_TEMPLATE.replace("__SHARED_SECRET__", actual_secret.replace('"', '\\"'))
    out = output_path or Path.cwd() / DEFAULT_SCRIPT_NAME
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(content)
    return out, actual_secret


# --------------------------------------------------------------------------- #
# CLI entry point (`pyalert-setup`)
# --------------------------------------------------------------------------- #

def _cmd_test(cfg: Config) -> int:
    problems = cfg.validate()
    if problems:
        print("[pyalert] Cannot send test email — fix these issues first:")
        for p in problems:
            print(f"  - {p}")
        return 1

    from pyalert.notifier import PyAlert  # local import: avoids cycle at module load

    alert = PyAlert(config=cfg, project_name="pyalert-setup test")
    ok = alert.test_connection()
    return 0 if ok else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyalert-setup",
        description="Configure pyalert credentials and generate the Gmail Apps Script bridge.",
    )
    parser.add_argument(
        "--generate-script",
        action="store_true",
        help="Write a ready-to-deploy Code.gs Apps Script file to the current directory.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for --generate-script (default: ./Code.gs).",
    )
    parser.add_argument(
        "--shared-secret",
        type=str,
        default=None,
        help="Shared secret to embed in the generated script / save to config.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip the interactive wizard; only apply flags/env vars and save.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the current saved configuration (secret redacted) and exit.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a real test digest email using the saved configuration.",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.show:
        cfg = load_config()
        redacted = cfg.to_dict()
        if redacted.get("shared_secret"):
            redacted["shared_secret"] = "*" * 8
        print(json.dumps(redacted, indent=2, sort_keys=True))
        print(f"\nConfig file: {CONFIG_FILE}")
        return 0

    if args.generate_script:
        out_path = Path(args.output) if args.output else None
        path, token = generate_apps_script(shared_secret=args.shared_secret, output_path=out_path)
        print(f"\n✅ Wrote Apps Script bridge to: {path}")
        print(f"Embedded Shared Secret:         {token}")
        print(
            "\nNext steps:\n"
            "  1. Open https://script.google.com/ and create a new project.\n"
            f"  2. Paste the contents of {path} into Code.gs.\n"
            "  3. Deploy -> New deployment -> Select type: Web app\n"
            "       - Execute as: Me\n"
            "       - Who has access: Anyone  (Secured via SHARED_SECRET)\n"
            "  4. Authorize Gmail access when prompted.\n"
            "  5. Copy the Web app URL and run `pyalert-setup` to save your configuration.\n"
        )
        return 0

    if args.test:
        cfg = load_config()
        return _cmd_test(cfg)

    existing = load_config()
    if args.non_interactive:
        if args.shared_secret is not None:
            existing.shared_secret = args.shared_secret
        problems = existing.validate()
        save_config(existing)
        if problems:
            print("[pyalert] Saved with warnings:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"✅ Saved configuration to {CONFIG_FILE}")
        return 0

    run_wizard(existing)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())