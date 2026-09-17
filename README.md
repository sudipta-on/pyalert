# pyalert

[![PyPI version](https://img.shields.io/pypi/v/pyalert.svg)](https://pypi.org/project/pyalert/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyalert.svg)](https://pypi.org/project/pyalert/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/pyalert.svg)](https://pypi.org/project/pyalert/)

**Get emailed when your training run finishes, stalls, or crashes — without ever handing an email password or a paid API key to a Python package.**

`pyalert` is a lightweight alerting library for researchers, ML engineers, and anyone running long background jobs (training loops, simulations, HPC batch jobs). It buffers your progress checkpoints into a clean HTML digest, sends immediate alerts on crashes with a full traceback, and does it all through a tiny Google Apps Script bridge that runs **in your own Google account** — pyalert itself never touches your Gmail credentials.

---

## Why pyalert?

| | |
|---|---|
| 🔐 **Zero-credential-leak** | Sends mail via a Google Apps Script Web App deployed under **your** Google account. No SMTP password, no Gmail "app password", no third-party API key ever stored or transmitted by pyalert. |
| 🪶 **Lightweight** | Pure standard library except for one hard dependency: `psutil`. NVIDIA GPU stats are read via raw `ctypes` bindings to NVML — no `pynvml`, no `torch` required. |
| 🧵 **Non-blocking** | Emails dispatch on a background daemon thread by default; your training loop never waits on the network. |
| 🧺 **Smart batching** | Frequent `checkpoint()` calls in tight loops are buffered and merged into one digest email per cooldown window (default 60s), instead of spamming your inbox. |
| 🚨 **Instant crash alerts** | Errors and exceptions bypass the cooldown entirely and are sent immediately with a full stack trace. |
| 📎 **Attachments** | Attach plots, CSVs, or log files — base64-encoded and delivered as real Gmail attachments. |
| 🖥️ **Cross-platform** | Linux, macOS, and Windows, with correct config paths and permissions on each. |
| 🧯 **Fails safe** | Network errors, missing GPU drivers, or bad file paths are caught and logged to stderr — pyalert will never crash or block your actual computation. |

---

## Architecture

```
                      YOUR MACHINE (laptop / HPC node / cloud VM)
   ┌─────────────────────────────────────────────────────────────────┐
   │                                                                 │
   │   your_script.py                                                │
   │   ┌───────────────────────────────────────────────────────────┐ │
   │   │  from pyalert import PyAlert                              │ │
   │   │  alert = PyAlert(project_name="training-run")             │ │
   │   │                                                           │ │
   │   │                         ─┐                                │ │
   │   │  def train(): ...        │ decorator / context manager /  │ │
   │   │                          │  manual .checkpoint() calls    │ │
   │   │  with alert.track_block()│                                │ │
   │   │  alert.checkpoint(...)  ─┘                                │ │
   │   └──────────────────────┬────────────────────────────────────┘ │
   │                          │                                      │
   │                          ▼                                      │
   │   ┌─────────────────────────────────────────────────────────┐   │
   │   │  pyalert.notifier.PyAlert                               │   │
   │   │  ┌───────────────┐    ┌───────────────┐   ┌───────────┐ │   │
   │   │  │ Rate limiter/ │    │ HTML digest   │   │ Attachment│ │   │
   │   │  │ event buffer  │──▶│ renderer      │──▶│ base64    │ │   │
   │   │  │ (cooldown,    │    │ (inline-CSS,  │   │ encoder   │ │   │
   │   │  │ atexit flush) │    │ mobile-safe)  │   │           │ │   │
   │   │  └───────────────┘    └───────────────┘   └───────────┘ │   │
   │   └────────────────────────────┬────────────────────────────┘   │
   │                                │  background thread             │
   │   ┌─────────────────────────┐  │                                │
   │   │ pyalert.monitor         │  │                                │
   │   │ SystemMonitor           │  │                                │
   │   │  • psutil: CPU/RAM/disk │  │                                │
   │   │  • ctypes → NVML: GPU   │  │                                │
   │   └─────────────────────────┘  │                                │
   │                                ▼                                │
   │                    HTTPS POST (JSON payload)                    │
   └────────────────────────────────┼────────────────────────────────┘
                                    │
                                    ▼
                 ┌────────────────────────────────────────┐
                 │  Google Apps Script Web App (Code.gs)  │
                 │  deployed under YOUR Google account    │
                 │  • validates optional shared secret    │
                 │  • decodes attachments                 │
                 │  • GmailApp.sendEmail(...)             │  ← runs as YOU,
                 └───────────────────┬────────────────────┘   OAuth-authorized
                                     │                          by Google, no
                                     ▼                          password ever
                        📧 Styled HTML email in your inbox     passed around
```

Because the Apps Script runs *inside* Google's infrastructure under your own account, Gmail sending is authorized via Google's own OAuth consent screen when you deploy it — pyalert (the Python client) only ever sees a webhook URL that you control, and can be revoked or redeployed at any time from `script.google.com`.

---

## Installation

```bash
pip install pyalert
```

Requires Python 3.8+. The only hard runtime dependency is `psutil`.

---

## Quickstart

### 1. Deploy your personal email bridge (one-time, ~2 minutes)

```bash
pyalert-setup --generate-script
```

This writes a ready-to-paste `Code.gs` file and prints deployment instructions:

1. Go to [script.google.com](https://script.google.com/) → **New project**.
2. Delete the boilerplate, paste in the generated `Code.gs`.
3. **Deploy → New deployment → Web app**
   - Execute as: **Me**
   - Who has access: **Only myself** (recommended)
4. Click **Deploy**, authorize the requested Gmail permission (this is Google's own OAuth screen — pyalert never sees this token).
5. Copy the **Web app URL**.

### 2. Run the setup wizard

```bash
pyalert-setup
```

Paste in the Web App URL, your recipient email, and (optionally) a shared secret — this saves `~/.config/pyalert/config.json`. Verify it works:

```bash
pyalert-setup --test
```

### 3. Use it in your code

```python
from pyalert import PyAlert

alert = PyAlert(project_name="resnet50-imagenet")

def train_model():
    for epoch in range(100):
        loss = run_epoch()
        alert.checkpoint(
            f"epoch {epoch} complete",
            extra={"loss": round(loss, 4), "epoch": epoch},
        )

train_model()  # ▶/✔ notifications + a final digest email; a crash alert
               # with full traceback fires immediately if anything raises
```

---

## Usage patterns

### Decorator

```python
@alert.watch(project="data-pipeline", capture_result=True)
def build_dataset():
    ...
    return dataset_stats
```

### Context manager

```python
with alert.track_block("hyperparameter-search", attachments=["sweep_results.csv"]):
    run_sweep()
```

### Manual checkpoints (buffered + throttled)

```python
alert.checkpoint("validation accuracy improved", extra={"val_acc": 0.94})
```

### Manual crash reporting

```python
try:
    risky_operation()
except Exception:
    alert.report_exception(context="risky_operation failed")
    raise
```

### Attachments

```python
alert.checkpoint(
    "training complete — see attached loss curve",
    attachments=["loss_curve.png", "metrics.csv"],
)
```

Attachments over `max_attachment_mb` (default 20MB, configurable) are skipped with a warning rather than failing the send.

### `logging` integration

```python
import logging
from pyalert import PyAlert, PyAlertLogHandler

alert = PyAlert(project_name="etl-job")
logging.getLogger().addHandler(PyAlertLogHandler(alert, level=logging.WARNING))
```

Any `logging.warning()`/`error()`/`critical()` call anywhere in your codebase now also feeds pyalert's digest/crash pipeline — no code changes needed elsewhere.

### Graceful SIGINT/SIGTERM handling (opt-in)

```python
alert = PyAlert(project_name="hpc-job", catch_signals=True)
```

On `Ctrl+C` or a scheduler-issued `SIGTERM`, pyalert flushes a final "interrupted" alert before letting the signal proceed normally.

---

## Configuration

`pyalert-setup` writes `~/.config/pyalert/config.json`. Every field can also be overridden with an environment variable, useful for CI/HPC job schedulers:

| Config field | Environment variable | Default |
|---|---|---|
| `webhook_url` | `PYALERT_WEBHOOK_URL` | — |
| `recipient_email` | `PYALERT_RECIPIENT` | — |
| `sender_name` | `PYALERT_SENDER_NAME` | `PyAlert` |
| `shared_secret` | `PYALERT_SHARED_SECRET` | — |
| `default_cooldown_seconds` | `PYALERT_COOLDOWN` | `60` |
| `dry_run` | `PYALERT_DRY_RUN` | `false` |
| — | `PYALERT_CONFIG_DIR` | `~/.config/pyalert` |

`PYALERT_DRY_RUN=1` makes pyalert print what it *would* send to stderr instead of making a network call — handy for testing pipelines without spamming your inbox.

### CLI reference

```bash
pyalert-setup                    # interactive wizard
pyalert-setup --generate-script  # write Code.gs
pyalert-setup --test             # send a real test email with current config
pyalert-setup --show             # print current config (secret redacted)
pyalert-setup --non-interactive --shared-secret XYZ   # scriptable/CI use
```

---

## What's in the digest email

Every digest includes:

- **Event cards** — one per checkpoint, color-coded by level (`INFO`, `SUCCESS`, `WARNING`, `ERROR`, `CRITICAL`), with your message, any `extra={}` metrics in a monospace table, and (for crashes) the full formatted traceback.
- **System snapshot** — host name, platform, per-core CPU%, load average, system RAM, aggregate process RSS (including child processes), disk usage, cumulative network I/O, host uptime, and per-GPU utilization/memory/temperature/power for every detected NVIDIA GPU.
- Responsive, inline-CSS HTML that renders correctly in Gmail (web + mobile), Apple Mail, and Outlook.

---

## GPU monitoring details

`pyalert.monitor.SystemMonitor` loads `libnvidia-ml.so.1` (Linux) or `nvml.dll` (Windows) directly via `ctypes` — the same library `nvidia-smi` uses — and calls a minimal subset of the NVML C API (`nvmlDeviceGetCount_v2`, `nvmlDeviceGetHandleByIndex_v2`, `nvmlDeviceGetMemoryInfo`, `nvmlDeviceGetUtilizationRates`, `nvmlDeviceGetTemperature`, `nvmlDeviceGetPowerUsage`). If the library isn't found (no NVIDIA GPU, no driver, or macOS, which has no NVML), pyalert logs a one-time informational note to stderr and simply omits GPU data — your job is never interrupted.

---

## Security notes

- pyalert **never** asks for, stores, or transmits a Gmail password or app-specific password.
- The only secret pyalert stores locally is an optional **shared secret** you generate yourself, used to stop random requests to your Apps Script URL from sending mail on your behalf. `config.json` is written with owner-only file permissions (`0600`) on POSIX systems.
- Set the Apps Script deployment's access to **"Only myself"** for the strongest guarantee — only requests carrying your Google session (impossible for pyalert to forge) or the URL + shared secret can trigger a send.
- All payloads are sent over HTTPS to `script.google.com`, Google's own domain.

---

## Project layout

```
pyalert-runner/
├── pyproject.toml
├── LICENSE
├── README.md
└── pyalert/
    ├── __init__.py     # public API surface
    ├── config.py       # Config model, wizard, Code.gs generator, CLI (no cyclic imports)
    ├── monitor.py       # ctypes NVML GPU tracker + psutil CPU/RAM/disk/network sampler
    └── notifier.py      # PyAlert engine: buffering, HTML rendering, dispatch, decorator/CM
```

Import graph is strictly acyclic: `config.py` has zero imports from `monitor.py` or `notifier.py`; `monitor.py` has zero imports from `notifier.py`; `notifier.py` imports from both. `pyalert-setup` (→ `config.main`) only pulls in `notifier` lazily, inside the `--test` code path, so the CLI stays fast and dependency-light for the common config-only case.

---

## Contributing

Issues and PRs welcome. Run the test suite with:

```bash
pip install -e ".[dev]"
pytest
mypy pyalert
```

## License

MIT — see [LICENSE](LICENSE).
