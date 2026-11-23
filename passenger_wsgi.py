"""Passenger WSGI entry point for cPanel's "Setup Python App".

cPanel/Passenger expects a module named ``passenger_wsgi`` in the application
root that exposes a WSGI callable named ``application``. Configuration is loaded
lazily on the first request so a missing secret produces one clear CRITICAL log
line and a safe 503 instead of an opaque Passenger error page.

Do not rename this file unless the cPanel Python application settings are
changed to match (documented in CPANEL-DEPLOYMENT.md).
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from kalampeach.web.app import application  # noqa: E402  (import after sys.path setup)

__all__ = ["application"]
