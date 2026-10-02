"""Pytest configuration for the custom integration source tree."""

from __future__ import annotations

import sys
from pathlib import Path

import homeassistant  # Initialize HA's probatio alias before test imports voluptuous.

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
