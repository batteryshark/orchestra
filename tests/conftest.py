"""Shared test fixtures and import path setup.

The project intentionally has no third-party dependencies; the test layout just
keeps imports from orchestra_cli.usage and orchestra_cli.cli off the global
package namespace.
"""
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
