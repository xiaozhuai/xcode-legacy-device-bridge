#!/usr/bin/env python3
"""Compile/probe in a new temporary directory; never modifies either Xcode app."""
import argparse
import importlib.util
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge", root / "scripts/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--donor", required=True)
parser.add_argument("--target", required=True)
args = parser.parse_args()
bridge.host_check()
donor, target = bridge.app_path(args.donor), bridge.app_path(args.target)
bridge.validate_pair(donor, target)
bridge.validate_versions(donor, target)
output = Path(tempfile.mkdtemp(prefix="xcode-bridge-smoke-")).resolve()
print("Generated test artifacts retained at:", output, flush=True)
bridge.build_payload(donor, target, output / "payload/Contents", output, bridge.Runner(output / "commands.log"))
print("PASS: staged compile/sign/dlopen. Neither Xcode app was modified; no device test was performed.")
