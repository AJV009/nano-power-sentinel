#!/usr/bin/env python3
"""box-agent entry point.  Stdlib only."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from box_agent.web import serve  # noqa: E402

if __name__ == "__main__":
    serve()
