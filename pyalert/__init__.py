"""
pyalert
=======

Zero-credential-leak email alerting for long-running ML/HPC jobs.

Quickstart
----------
    from pyalert import PyAlert

    alert = PyAlert(project_name="resnet50-training")

    def train():
        for epoch in range(100):
            ...
            alert.checkpoint(f"epoch {epoch} done", extra={"loss": loss})

    train()

See the README for the full setup guide (``pyalert-setup``) and API
reference.
"""

from pyalert.config import Config, load_config, save_config
from pyalert.monitor import GPUInfo, SystemMonitor, SystemSnapshot
from pyalert.notifier import (
    Attachment,
    CheckpointEntry,
    PyAlert,
    PyAlertLogHandler,
    track_block,
)

__version__ = "1.0.0"

__all__ = [
    "PyAlert",
    "PyAlertLogHandler",
    "track_block",
    "Attachment",
    "CheckpointEntry",
    "Config",
    "load_config",
    "save_config",
    "SystemMonitor",
    "SystemSnapshot",
    "GPUInfo",
    "__version__",
]
