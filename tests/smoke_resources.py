#!/usr/bin/env python3
"""Compile minimal iOS resources using a generated Xcode; no app/device installation."""
import argparse
import importlib.util
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge", root / "scripts/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--target", required=True, help="Generated experimental Xcode 27 .app")
args = parser.parse_args()
bridge.host_check()
target = bridge.app_path(args.target)
if bridge.version(target) != bridge.TARGET_VERSION or not all(
        (target / "Contents" / p).is_file() for p in bridge.SHIMS):
    raise bridge.BridgeError("Expected a generated experimental Xcode 27 copy")
output = Path(tempfile.mkdtemp(prefix="xcode-bridge-resource-smoke-")).resolve()
print("Generated test artifacts retained at:", output, flush=True)
run = bridge.Runner(output / "commands.log")
bridge.check_build_tools(target, run)
environment = {"DEVELOPER_DIR": str(target / "Contents/Developer")}
bin_dir = target / "Contents/Developer/usr/bin"
nib = output / "BridgeSmoke.nib"
print("[compile] minimal XIB", flush=True)
print(run([bin_dir / "ibtool", "--compile", nib, root / "tests/fixtures/BridgeSmoke.xib",
           "--minimum-deployment-target", "16.0", "--target-device", "iphone", "--target-device", "ipad"],
          timeout=180, env_overrides=environment).strip())
if not nib.exists():
    raise bridge.BridgeError("ibtool did not create the compiled nib")
assets = output / "assets"
assets.mkdir()
print("[compile] minimal asset catalog", flush=True)
print(run([bin_dir / "actool", root / "tests/fixtures/BridgeSmoke.xcassets", "--compile", assets,
           "--platform", "iphoneos", "--minimum-deployment-target", "16.0", "--target-device", "iphone",
           "--target-device", "ipad", "--output-format", "human-readable-text"],
          timeout=180, env_overrides=environment).strip())
if not (assets / "Assets.car").is_file():
    raise bridge.BridgeError("actool did not create Assets.car")
print("PASS: tool startup and minimal XIB/asset compilation. Xcode was not modified; no device app was installed.")
