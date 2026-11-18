# Installation

Python 3.10+; no third-party runtime dependencies.

```text
python -m venv .venv
.venv/Scripts/python -m pip install --no-index --no-build-isolation .
.venv/Scripts/python -m jqa_skill doctor
python scripts/install.py --project /path/to/project
```
On Unix use `.venv/bin/python`. Replace project path with your actual directory. Refresh Codex and invoke `$journal-quality-assessment`. Live Codex recognition remains unverified. See README and references/public_workflow.md.
