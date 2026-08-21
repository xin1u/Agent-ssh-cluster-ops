#!/usr/bin/env python3
"""Compatibility launcher for the canonical GridLatch CLI."""

from pathlib import Path


_CANONICAL = Path(__file__).resolve().parents[1] / "skills" / "ssh-cluster-ops" / "scripts" / "clusterctl.py"
exec(compile(_CANONICAL.read_bytes(), str(_CANONICAL), "exec"), globals(), globals())
