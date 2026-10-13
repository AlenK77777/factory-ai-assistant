"""Point the event log at a temp file for the whole pytest run.

logs/events.log is the live chat log. Tests must not append to it.
"""

import os
import tempfile
from pathlib import Path

_log_dir = Path(tempfile.mkdtemp(prefix="factory-pytest-events-"))
os.environ["FACTORY_EVENT_LOG"] = str(_log_dir / "events.log")
