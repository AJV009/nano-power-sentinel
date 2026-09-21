"""box-agent - read-only telemetry endpoint for the W7900 box.

Design contract, in order of importance:

  1. NEVER BLOCK. The jetson's collector polls every 5 s with a hard timeout.
     Every sysfs read is wrapped; a failed read yields null for that field
     rather than an error for the whole response.
  2. READ-ONLY, UNPRIVILEGED. Config writes are phase 7 and are not here yet.
  3. ABSENCE IS A THIRD STATE. Unreadable is null -- never 0, never stale.
"""
__version__ = "1.0"
