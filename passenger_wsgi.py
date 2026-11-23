"""Passenger WSGI entry point for cPanel.

cPanel's Setup Python App / Passenger facility looks for ``passenger_wsgi.py`` in
the application root and expects a module-level ``application`` callable.

The file only prepares ``sys.path``; all configuration, validation and routing
live in :mod:`app.wsgi`, which is also what tests exercise directly.
"""

from __future__ import annotations

import os
import sys

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from app.wsgi import application  # noqa: E402  (path must be set first)

__all__ = ["application"]
