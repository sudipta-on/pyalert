"""
pyalert.notifier
=================

The core pyalert engine: buffering/throttling, HTML digest rendering,
attachment encoding, and dispatch to the user's Apps Script webhook bridge.

Public surface
--------------
- ``PyAlert``             : the main engine class.
- ``@watch``               : decorator that wraps a function with
                             start/finish/crash notifications.
- ``track_block``          : context manager equivalent of ``@watch`` for
                             arbitrary code blocks.
- ``PyAlertLogHandler``    : optional ``logging.Handler`` bridge.
"""

from __future__ import annotations

import atexit
import base64
import functools
import html as html_lib
import json
import logging
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from pyalert.config import Config, load_config
from pyalert.monitor import SystemMonitor, SystemSnapshot

F = TypeVar("F", bound=Callable[..., Any])

_LEVEL_COLORS = {
    "DEBUG": "#6b7280",
    "INFO": "#2563eb",
    "SUCCESS": "#16a34a",
    "WARNING": "#d97706",
    "ERROR": "#dc2626",
    "CRITICAL": "#7f1d1d",
}
_URGENT_LEVELS = {"ERROR", "CRITICAL"}


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #

@dataclass
class Attachment:
    filename: str
    content_base64: str
    mime_type: str
    size_bytes: int


@dataclass
class CheckpointEntry:
    timestamp: float
    level: str
    message: str
    extra: Dict[str, Any] = field(default_factory=dict)
    snapshot: Optional[Dict[str, Any]] = None
    traceback_str: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)

    def iso_time(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# --------------------------------------------------------------------------- #
# Main engine
# --------------------------------------------------------------------------- #

class PyAlert:
    """
    The pyalert notification engine.

    Parameters
    ----------
    config:
        A pre-built ``Config``. If omitted, loads from
        ``~/.config/pyalert/config.json`` (plus environment overrides).
    project_name:
        Friendly label included in every email subject, e.g. "resnet50-run3".
    cooldown:
        Overrides ``config.default_cooldown_seconds`` for this instance.
    track_gpu / track_disk / track_network:
        Toggle individual SystemMonitor sub-collectors.
    async_dispatch:
        If True (default), network sends happen on a background daemon
        thread so ``checkpoint()`` calls never block the training loop.
    catch_signals:
        If True, installs SIGINT/SIGTERM handlers that flush a final
        "interrupted" alert before re-raising the default behavior.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        project_name: Optional[str] = None,
        cooldown: Optional[int] = None,
        track_gpu: bool = True,
        track_disk: bool = True,
        track_network: bool = True,
        async_dispatch: bool = True,
        catch_signals: bool = False,
    ) -> None:
        self.config = config or load_config()
        self.project_name = project_name or os.path.basename(sys.argv[0]) or "pyalert-job"
        self.cooldown = cooldown if cooldown is not None else self.config.default_cooldown_seconds
        self.async_dispatch = async_dispatch

        self.monitor = SystemMonitor(
            track_gpu=track_gpu, track_disk=track_disk, track_network=track_network
        )

        self.run_id = uuid.uuid4().hex[:8]
        self._start_time = time.time()

        self._lock = threading.RLock()
        self._buffer: List[CheckpointEntry] = []
        self._last_sent: float = 0.0
        self._closed = False
        self._pending_threads: List[threading.Thread] = []

        problems = self.config.validate()
        if problems:
            sys.stderr.write(
                "[pyalert] WARNING: configuration incomplete — alerts will be "
                "skipped until you run `pyalert-setup`. Issues:\n"
                + "\n".join(f"  - {p}" for p in problems) + "\n"
            )

        atexit.register(self._on_exit)

        if catch_signals:
            self._install_signal_handlers()

    # ------------------------------------------------------------------ #
    # Signal handling (opt-in)
    # ------------------------------------------------------------------ #

    def _install_signal_handlers(self) -> None:
        original_handlers: Dict[int, Any] = {}

        def handler(signum: int, frame: Any) -> None:
            sig_name = signal.Signals(signum).name
            self.checkpoint(
                f"Process received {sig_name} — flushing pending alerts before exit.",
                level="WARNING",
                force=True,
            )
            self.flush(wait=True)
            original = original_handlers.get(signum, signal.SIG_DFL)
            if callable(original):
                original(signum, frame)
            else:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                original_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # e.g. not in main thread, or unsupported platform/signal
                pass

    # ------------------------------------------------------------------ #
    # Public API: checkpoint / crash / flush
    # ------------------------------------------------------------------ #

    def checkpoint(
        self,
        message: str,
        level: str = "INFO",
        extra: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[str]] = None,
        force: bool = False,
    ) -> None:
        """
        Record a checkpoint. Buffered checkpoints are coalesced into a
        single digest email once the cooldown window elapses (or
        immediately, if `level` is ERROR/CRITICAL or `force=True`).
        """
        level = level.upper()
        entry = CheckpointEntry(
            timestamp=time.time(),
            level=level,
            message=message,
            extra=extra or {},
            snapshot=self._safe_snapshot(),
            attachments=self._encode_attachments(attachments),
        )

        with self._lock:
            self._buffer.append(entry)
            should_flush_now = (
                force or level in _URGENT_LEVELS or self._cooldown_elapsed()
            )

        if should_flush_now:
            self.flush()

    def report_exception(
        self,
        exc: Optional[BaseException] = None,
        context: Optional[str] = None,
        attachments: Optional[List[str]] = None,
    ) -> None:
        """
        Immediately dispatch a crash alert with a full stack trace,
        bypassing the cooldown entirely. Safe to call from an
        ``except`` block with no arguments (uses ``sys.exc_info()``).
        """
        if exc is None:
            exc_type, exc_value, exc_tb = sys.exc_info()
            tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            message = f"{context + ': ' if context else ''}{exc_type.__name__ if exc_type else 'Unknown error'}: {exc_value}"
        else:
            tb_str = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            message = f"{context + ': ' if context else ''}{type(exc).__name__}: {exc}"

        entry = CheckpointEntry(
            timestamp=time.time(),
            level="CRITICAL",
            message=message,
            snapshot=self._safe_snapshot(),
            traceback_str=tb_str,
            attachments=self._encode_attachments(attachments),
        )
        with self._lock:
            self._buffer.append(entry)
        # Crashes always bypass the rate limiter.
        self.flush()

    def flush(self, wait: bool = False) -> None:
        """Dispatch all buffered checkpoints as one digest email, now."""
        with self._lock:
            if not self._buffer:
                return
            entries = self._buffer
            self._buffer = []
            self._last_sent = time.time()

        urgent = any(e.level in _URGENT_LEVELS for e in entries)
        if self.async_dispatch and not wait:
            t = threading.Thread(
                target=self._dispatch_safe, args=(entries, urgent), daemon=True
            )
            t.start()
            with self._lock:
                self._pending_threads.append(t)
        else:
            self._dispatch_safe(entries, urgent)

    def test_connection(self) -> bool:
        """Send a minimal test email synchronously. Returns True on success."""
        entry = CheckpointEntry(
            timestamp=time.time(),
            level="INFO",
            message="✅ This is a test alert from pyalert. If you can read this, your bridge is working!",
            snapshot=self._safe_snapshot(),
        )
        return self._dispatch([entry], urgent=False)

    # ------------------------------------------------------------------ #
    # Decorator / context manager
    # ------------------------------------------------------------------ #

    def watch(
        self,
        project: Optional[str] = None,
        cooldown: Optional[int] = None,
        notify_start: bool = True,
        capture_result: bool = False,
    ) -> Callable[[F], F]:
        """
        Decorator: sends a start checkpoint, a finish checkpoint (with
        elapsed time, and optionally the function's return value), and an
        immediate crash alert with full traceback on any uncaught
        exception, which is then re-raised unchanged.
        """

        def decorator(func: F) -> F:
            label = project or func.__name__

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if notify_start:
                    self.checkpoint(f"▶ Started '{label}'", level="INFO")
                started = time.time()
                try:
                    result = func(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - must catch everything to alert
                    self.report_exception(exc, context=f"'{label}' crashed")
                    raise
                elapsed = time.time() - started
                extra = {"elapsed_seconds": round(elapsed, 3)}
                if capture_result:
                    extra["result"] = _safe_repr(result)
                self.checkpoint(
                    f"✔ Finished '{label}' in {_format_duration(elapsed)}",
                    level="SUCCESS",
                    extra=extra,
                )
                return result

            return wrapper  # type: ignore[return-value]

        return decorator

    def track_block(self, name: str, attachments: Optional[List[str]] = None) -> "_TrackBlock":
        """Context manager version of ``watch`` for arbitrary code blocks."""
        return _TrackBlock(self, name, attachments)

    # ------------------------------------------------------------------ #
    # Internals: dispatch, rendering, attachments
    # ------------------------------------------------------------------ #

    def _cooldown_elapsed(self) -> bool:
        return (time.time() - self._last_sent) >= self.cooldown

    def _safe_snapshot(self) -> Optional[Dict[str, Any]]:
        try:
            return self.monitor.snapshot().as_dict()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[pyalert] WARNING: snapshot failed ({exc}).\n")
            return None

    def _encode_attachments(self, paths: Optional[List[str]]) -> List[Attachment]:
        if not paths:
            return []
        out = []
        for p in paths:
            att = self._encode_attachment(p)
            if att is not None:
                out.append(att)
        return out

    def _encode_attachment(self, path: str) -> Optional[Attachment]:
        try:
            if not os.path.isfile(path):
                sys.stderr.write(f"[pyalert] WARNING: attachment not found, skipping: {path}\n")
                return None
            size_bytes = os.path.getsize(path)
            max_bytes = self.config.max_attachment_mb * 1024 * 1024
            if size_bytes > max_bytes:
                sys.stderr.write(
                    f"[pyalert] WARNING: attachment '{path}' is "
                    f"{size_bytes / (1024 * 1024):.1f}MB, exceeding the "
                    f"{self.config.max_attachment_mb}MB limit — skipping.\n"
                )
                return None
            with open(path, "rb") as fh:
                content = fh.read()
            mime_type, _ = mimetypes.guess_type(path)
            return Attachment(
                filename=os.path.basename(path),
                content_base64=base64.b64encode(content).decode("ascii"),
                mime_type=mime_type or "application/octet-stream",
                size_bytes=size_bytes,
            )
        except OSError as exc:
            sys.stderr.write(f"[pyalert] WARNING: could not read attachment '{path}' ({exc}). Skipping.\n")
            return None

    def _dispatch_safe(self, entries: List[CheckpointEntry], urgent: bool) -> None:
        try:
            self._dispatch(entries, urgent)
        except Exception as exc:  # noqa: BLE001 - background thread must never crash the host
            sys.stderr.write(f"[pyalert] WARNING: failed to dispatch alert ({exc}).\n")

    def _dispatch(self, entries: List[CheckpointEntry], urgent: bool) -> bool:
        subject = self._build_subject(entries, urgent)
        html_body = render_digest_html(
            entries=entries,
            project_name=self.project_name,
            run_id=self.run_id,
            urgent=urgent,
        )
        all_attachments: List[Attachment] = []
        for e in entries:
            all_attachments.extend(e.attachments)

        return self._send_email(subject, html_body, all_attachments)

    def _build_subject(self, entries: List[CheckpointEntry], urgent: bool) -> str:
        prefix = "🚨 CRASH" if urgent else "📈 Digest"
        count = len(entries)
        plural = "s" if count != 1 else ""
        return f"[pyalert] {prefix} — {self.project_name} ({count} event{plural})"

    def _send_email(self, subject: str, html_body: str, attachments: List[Attachment]) -> bool:
        problems = self.config.validate()
        if problems:
            sys.stderr.write(
                "[pyalert] Skipping send — configuration invalid. Run `pyalert-setup`.\n"
            )
            return False

        payload = {
            "recipient_email": self.config.recipient_email,
            "sender_name": self.config.sender_name,
            "subject": subject,
            "html_body": html_body,
            "shared_secret": self.config.shared_secret,
            "attachments": [
                {
                    "filename": a.filename,
                    "content_base64": a.content_base64,
                    "mime_type": a.mime_type,
                }
                for a in attachments
            ],
        }
        body = json.dumps(payload).encode("utf-8")

        if self.config.dry_run:
            sys.stderr.write(
                f"[pyalert] DRY RUN — would send '{subject}' "
                f"({len(body) / 1024:.1f} KB payload, {len(attachments)} attachment(s)).\n"
            )
            return True

        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.retries + 1):
            try:
                req = urllib.request.Request(
                    self.config.webhook_url,
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    resp.read()  # drain
                    if 200 <= resp.status < 300:
                        return True
                    last_error = RuntimeError(f"HTTP {resp.status}")
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as exc:
                last_error = exc

            if attempt < self.config.retries:
                backoff = min(2 ** attempt, 30)
                sys.stderr.write(
                    f"[pyalert] WARNING: send attempt {attempt}/{self.config.retries} "
                    f"failed ({last_error}). Retrying in {backoff}s...\n"
                )
                time.sleep(backoff)

        sys.stderr.write(
            f"[pyalert] ERROR: failed to send email after {self.config.retries} attempts "
            f"({last_error}). Continuing without interrupting your job.\n"
        )
        return False

    def _on_exit(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with self._lock:
                has_pending = bool(self._buffer)
            if has_pending:
                self.checkpoint(
                    f"Process exiting after {_format_duration(time.time() - self._start_time)} total runtime.",
                    level="INFO",
                )
            # Synchronous final flush — atexit hooks can't reliably wait on
            # daemon threads once the interpreter starts tearing down.
            self.flush(wait=True)
        except Exception:  # noqa: BLE001 - never raise during interpreter shutdown
            pass
        finally:
            self.monitor.close()


# --------------------------------------------------------------------------- #
# Context manager helper
# --------------------------------------------------------------------------- #

class _TrackBlock:
    """Implementation object returned by ``PyAlert.track_block``."""

    def __init__(self, alert: PyAlert, name: str, attachments: Optional[List[str]]) -> None:
        self._alert = alert
        self._name = name
        self._attachments = attachments
        self._start = 0.0

    def __enter__(self) -> "_TrackBlock":
        self._start = time.time()
        self._alert.checkpoint(f"▶ Entered block '{self._name}'", level="INFO")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        elapsed = time.time() - self._start
        if exc_type is not None:
            self._alert.report_exception(
                exc_val, context=f"Block '{self._name}' crashed", attachments=self._attachments
            )
            return False  # do not suppress the exception
        self._alert.checkpoint(
            f"✔ Completed block '{self._name}' in {_format_duration(elapsed)}",
            level="SUCCESS",
            extra={"elapsed_seconds": round(elapsed, 3)},
            attachments=self._attachments,
        )
        return False


def track_block(alert: PyAlert, name: str, attachments: Optional[List[str]] = None) -> _TrackBlock:
    """Module-level convenience wrapper: ``track_block(alert, "phase1")``."""
    return alert.track_block(name, attachments=attachments)


# --------------------------------------------------------------------------- #
# Optional logging.Handler bridge
# --------------------------------------------------------------------------- #

class PyAlertLogHandler(logging.Handler):
    """
    A ``logging.Handler`` that forwards log records into a ``PyAlert``
    instance's checkpoint buffer. Attach to any logger to get automatic
    email digests of WARNING+ log lines (or any level you configure),
    without changing your logging calls at all::

        handler = PyAlertLogHandler(alert, level=logging.WARNING)
        logging.getLogger().addHandler(handler)
    """

    _LOGGING_TO_PYALERT = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def __init__(self, alert: PyAlert, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._alert = alert

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            level = self._LOGGING_TO_PYALERT.get(record.levelno, "INFO")
            if record.exc_info:
                self._alert.report_exception(context=message)
            else:
                self._alert.checkpoint(message, level=level)
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            self.handleError(record)


# --------------------------------------------------------------------------- #
# HTML digest rendering
# --------------------------------------------------------------------------- #

def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _safe_repr(value: Any, max_len: int = 300) -> str:
    try:
        r = repr(value)
    except Exception:  # noqa: BLE001
        r = "<unrepresentable value>"
    if len(r) > max_len:
        r = r[: max_len - 3] + "..."
    return r


def _esc(text: Any) -> str:
    return html_lib.escape(str(text), quote=True)


def _render_metric_row(label: str, value: Any, unit: str = "") -> str:
    if value is None:
        return ""
    return (
        '<tr>'
        f'<td style="padding:4px 10px 4px 0;color:#6b7280;font-size:13px;">{_esc(label)}</td>'
        f'<td style="padding:4px 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
        f'font-size:13px;color:#111827;">{_esc(value)}{_esc(unit)}</td>'
        '</tr>'
    )


def _render_snapshot_table(snapshot: Optional[Dict[str, Any]]) -> str:
    if not snapshot:
        return '<p style="color:#9ca3af;font-size:12px;">No system snapshot available.</p>'

    rows = []
    rows.append(_render_metric_row("Host", snapshot.get("hostname")))
    rows.append(_render_metric_row("Platform", snapshot.get("platform")))
    rows.append(_render_metric_row("Python", snapshot.get("python_version")))
    rows.append(_render_metric_row("PID", snapshot.get("pid")))
    if snapshot.get("cpu_percent_total") is not None:
        n_cores = len(snapshot.get("cpu_percent_per_core") or [])
        rows.append(_render_metric_row("CPU (avg)", snapshot["cpu_percent_total"], f"% across {n_cores} cores"))
    if snapshot.get("load_average"):
        rows.append(_render_metric_row("Load avg (1/5/15m)", ", ".join(str(x) for x in snapshot["load_average"])))
    if snapshot.get("ram_used_mb") is not None:
        rows.append(
            _render_metric_row(
                "System RAM",
                f"{snapshot['ram_used_mb']:.0f} / {snapshot['ram_total_mb']:.0f}",
                f" MB ({snapshot.get('ram_percent')}%)",
            )
        )
    if snapshot.get("process_rss_mb") is not None:
        rows.append(_render_metric_row("Process RSS (incl. children)", snapshot["process_rss_mb"], " MB"))
    if snapshot.get("disk_percent") is not None:
        rows.append(
            _render_metric_row(
                "Disk (cwd)",
                f"{snapshot['disk_used_gb']:.1f} / {snapshot['disk_total_gb']:.1f}",
                f" GB ({snapshot['disk_percent']}%)",
            )
        )
    if snapshot.get("net_sent_mb") is not None:
        rows.append(_render_metric_row("Network (cumulative)", f"↑{snapshot['net_sent_mb']:.0f} / ↓{snapshot['net_recv_mb']:.0f}", " MB"))
    if snapshot.get("uptime_seconds") is not None:
        rows.append(_render_metric_row("Host uptime", _format_duration(snapshot["uptime_seconds"])))

    gpu_rows = []
    for gpu in snapshot.get("gpus") or []:
        label = f"GPU {gpu.get('index')}: {gpu.get('name')}"
        bits = []
        if gpu.get("utilization_pct") is not None:
            bits.append(f"util {gpu['utilization_pct']:.0f}%")
        if gpu.get("memory_used_mb") is not None:
            mem_pct = ""
            if gpu.get("memory_total_mb"):
                mem_pct = f" ({gpu['memory_used_mb'] / gpu['memory_total_mb'] * 100:.0f}%)"
            bits.append(f"mem {gpu['memory_used_mb']:.0f}/{gpu.get('memory_total_mb', 0):.0f}MB{mem_pct}")
        if gpu.get("temperature_c") is not None:
            bits.append(f"{gpu['temperature_c']:.0f}°C")
        if gpu.get("power_watts") is not None:
            bits.append(f"{gpu['power_watts']:.0f}W")
        gpu_rows.append(_render_metric_row(label, ", ".join(bits)))

    if not snapshot.get("gpus"):
        gpu_rows.append(
            '<tr><td colspan="2" style="padding:4px 0;color:#9ca3af;font-size:12px;">'
            "No NVIDIA GPU detected.</td></tr>"
        )

    return (
        '<table style="width:100%;border-collapse:collapse;">'
        + "".join(rows)
        + '<tr><td colspan="2" style="padding-top:8px;"></td></tr>'
        + "".join(gpu_rows)
        + "</table>"
    )


def _render_entry_card(entry: CheckpointEntry) -> str:
    color = _LEVEL_COLORS.get(entry.level, "#2563eb")
    badge = (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f'background:{color}1A;color:{color};font-size:11px;font-weight:600;'
        f'letter-spacing:0.03em;text-transform:uppercase;">{_esc(entry.level)}</span>'
    )

    extra_html = ""
    if entry.extra:
        extra_rows = "".join(_render_metric_row(k, v) for k, v in entry.extra.items())
        extra_html = f'<table style="width:100%;border-collapse:collapse;margin-top:6px;">{extra_rows}</table>'

    tb_html = ""
    if entry.traceback_str:
        tb_html = (
            '<pre style="background:#111827;color:#f87171;padding:12px;border-radius:8px;'
            'overflow-x:auto;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
            f'font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;">{_esc(entry.traceback_str)}</pre>'
        )

    attachments_html = ""
    if entry.attachments:
        names = ", ".join(_esc(a.filename) for a in entry.attachments)
        attachments_html = (
            f'<p style="margin:6px 0 0;font-size:12px;color:#6b7280;">📎 Attached: {names}</p>'
        )

    return f"""
    <div style="border:1px solid #e5e7eb;border-left:4px solid {color};border-radius:10px;
                padding:14px 16px;margin-bottom:12px;background:#ffffff;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        {badge}
        <span style="font-size:11px;color:#9ca3af;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;">{_esc(entry.iso_time())}</span>
      </div>
      <p style="margin:0;font-size:14px;color:#111827;line-height:1.5;">{_esc(entry.message)}</p>
      {extra_html}
      {tb_html}
      {attachments_html}
    </div>
    """


def render_digest_html(
    entries: List[CheckpointEntry],
    project_name: str,
    run_id: str,
    urgent: bool,
) -> str:
    """
    Render the full HTML email body. Inline-styled, table-based layout for
    maximum compatibility with Gmail (web/mobile), Apple Mail, and Outlook.
    """
    header_color = "#dc2626" if urgent else "#2563eb"
    header_label = "🚨 Crash Alert" if urgent else "📈 Status Digest"

    entries_html = "".join(_render_entry_card(e) for e in entries)
    latest_snapshot = entries[-1].snapshot if entries else None
    snapshot_html = _render_snapshot_table(latest_snapshot)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>pyalert digest</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="width:600px;max-width:94vw;background:#ffffff;border-radius:14px;overflow:hidden;
                      box-shadow:0 1px 3px rgba(0,0,0,0.08);">
          <tr>
            <td style="background:{header_color};padding:20px 24px;">
              <p style="margin:0;color:#ffffff;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.85;">pyalert</p>
              <h1 style="margin:4px 0 0;color:#ffffff;font-size:20px;font-weight:700;">{_esc(header_label)}</h1>
              <p style="margin:6px 0 0;color:#ffffff;font-size:13px;opacity:0.9;">
                {_esc(project_name)} &nbsp;•&nbsp; run <span style="font-family:ui-monospace,Menlo,Consolas,monospace;">{_esc(run_id)}</span>
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 24px;">
              <h2 style="margin:0 0 12px;font-size:13px;color:#374151;text-transform:uppercase;letter-spacing:0.05em;">
                Events ({len(entries)})
              </h2>
              {entries_html}
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 20px;">
              <h2 style="margin:16px 0 10px;font-size:13px;color:#374151;text-transform:uppercase;letter-spacing:0.05em;">
                System Snapshot
              </h2>
              <div style="border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;background:#f9fafb;">
                {snapshot_html}
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;">
              <p style="margin:0;font-size:11px;color:#9ca3af;">
                Sent by pyalert via your own Google Apps Script bridge — no third-party service ever
                saw your Gmail credentials. Run <span style="font-family:monospace;">pyalert-setup</span> to reconfigure.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
