#!/usr/bin/env python3
"""Non-live fixture used to smoke-test the benchmark runner."""

from pathlib import Path

secret = Path(__file__).with_name("input.txt").read_text(encoding="utf-8").strip()
print(f"flag{{{secret}}}")
