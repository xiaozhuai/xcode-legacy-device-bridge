#!/usr/bin/env python3
"""Create a patched Xcode 27 copy from untouched Xcode 26.6 and 27.0 sources. Python 3.9+."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError

ROOT = Path(__file__).resolve().parent.parent
DONOR_VERSION = ("26.6", "17F113")
TARGET_VERSION = ("27.0", "27A266a")
FRAMEWORKS = (
    "PlugIns/IDEiOSSupportCore.framework",
    "SharedFrameworks/DTDeviceKitBase.framework",
    "SharedFrameworks/DVTDeviceFoundation.framework",
)
# These native-27 tools load IDEFoundation/IDEKit, directly or through ibtoold.
# Each independent process must be able to load the ad-hoc legacy framework.
BUILD_TOOLS = tuple("Developer/usr/bin/" + name for name in
                    ("ibtool", "actool", "ibtoold", "ictool", "momc", "mapc", "intentbuilderc"))
ENTRIES = ("MacOS/Xcode", "Developer/usr/bin/xcodebuild", "Developer/usr/bin/xcdevice") + BUILD_TOOLS
DEVICE_SUPPORT = "Developer/Platforms/iPhoneOS.platform/DeviceSupport"
SHIMS = tuple("SharedFrameworks/libIOS16%sShim.dylib" % p for p in
              ("DVTFoundation", "IDEFoundation", "XCTest", "DVTMacroFoundation"))
REQUIRED_ORIGINALS = FRAMEWORKS + ENTRIES + ("_CodeSignature",)


class BridgeError(Exception):
    pass


def say(message):
    print(message, flush=True)


def within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def app_path(value):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir() or path.suffix != ".app" or not (path / "Contents/version.plist").is_file():
        raise BridgeError("Not an Xcode app bundle: %s" % path)
    with (path / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != "com.apple.dt.Xcode" or info.get("CFBundleExecutable") != "Xcode":
        raise BridgeError("Bundle identity is not Xcode: " + str(path))
    return path


def output_path(value, sources):
    requested = Path(value).expanduser()
    if os.path.lexists(requested):
        raise BridgeError("Output already exists; it will not be overwritten: " + str(requested))
    path = requested.resolve()
    if path.suffix != ".app":
        raise BridgeError("Output must be a new .app path: " + str(path))
    if "Applications" in path.parts or any(within(path, Path(p)) for p in ("/System", "/Library")):
        raise BridgeError("Create the experimental output outside Applications/System/Library.")
    for source in sources:
        if within(path, source) or within(source, path):
            raise BridgeError("Output must be separate from both source app bundles.")
    return path


def validate_target_roots(target, artifacts=()):
    for relative in REQUIRED_ORIGINALS + SHIMS + (DEVICE_SUPPORT,) + tuple(artifacts):
        artifact = target / "Contents" / relative
        if artifact.is_symlink() or not within(artifact.parent.resolve(), target):
            raise BridgeError("Artifact escapes the app or is symlinked: " + str(artifact))
    if (target / "Contents").is_symlink():
        raise BridgeError("Symlinked app Contents is not supported.")


def validate_pair(donor, target):
    if donor == target or within(donor, target) or within(target, donor):
        raise BridgeError("Xcode 26 and Xcode 27 sources must be separate, non-nested app bundles.")


def donor_device_support(donor):
    """Enumerate every direct entry, not just the 16.4 disk image directory."""
    root = donor / "Contents" / DEVICE_SUPPORT
    if root.is_symlink() or not root.is_dir() or not within(root.resolve(), donor):
        raise BridgeError("Missing, symlinked or external donor DeviceSupport root: " + str(root))
    entries = sorted(root.iterdir(), key=lambda path: path.name)
    if not entries:
        raise BridgeError("Donor DeviceSupport is empty: " + str(root))
    for entry in entries:
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise BridgeError("Unsupported or symlinked donor DeviceSupport entry: " + str(entry))
    return tuple(DEVICE_SUPPORT + "/" + entry.name for entry in entries)


def version(app):
    with (app / "Contents/version.plist").open("rb") as stream:
        data = plistlib.load(stream)
    return data.get("CFBundleShortVersionString"), data.get("ProductBuildVersion")


def validate_versions(donor, target):
    if version(donor) != DONOR_VERSION or version(target) != TARGET_VERSION:
        raise BridgeError("Only donor %s/%s -> source %s/%s is supported; found %r -> %r."
                          % (*DONOR_VERSION, *TARGET_VERSION, version(donor), version(target)))


def host_check(writing=False):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BridgeError("This experiment requires Apple Silicon macOS; Intel is not supported.")
    release = subprocess.check_output(["/usr/bin/sw_vers", "-productVersion"], text=True).strip()
    if release.split(".")[0] != "27":
        raise BridgeError("Only macOS 27 is supported by this pinned recipe; found " + release)
    if writing and os.geteuid() == 0:
        raise BridgeError("Do not run as root/sudo. Use a writable output path and App Management permission.")


def assert_not_running(target):
    output = subprocess.check_output(["/bin/ps", "-axo", "pid=,command="], text=True)
    prefix = str(target / "Contents") + "/"
    for line in output.splitlines():
        match = re.match(r"\s*\d+\s+(.+)", line)
        if match and match.group(1).startswith(prefix):
            raise BridgeError("Quit this Xcode and its tools/debug sessions first: " + match.group(1))


def fingerprint(path):
    """Content, relative paths, symlink targets and modes, without following symlinks."""
    if not os.path.lexists(path):
        return None
    entries = [path]
    if path.is_dir() and not path.is_symlink():
        for directory, dirs, files in os.walk(path, followlinks=False):
            entries.extend(Path(directory) / name for name in dirs + files)
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda p: str(p.relative_to(path))):
        info = entry.lstat()
        label = str(entry.relative_to(path))
        if stat.S_ISLNK(info.st_mode):
            data = "link:" + os.readlink(entry)
        elif stat.S_ISREG(info.st_mode):
            file_digest = hashlib.sha256()
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            data = "file:" + file_digest.hexdigest()
        elif stat.S_ISDIR(info.st_mode):
            data = "dir"
        else:
            raise BridgeError("Unsupported artifact type: " + str(entry))
        digest.update(json.dumps([label, stat.S_IMODE(info.st_mode), data]).encode("utf-8") + b"\n")
    return digest.hexdigest()


def bundle_files(app):
    """List regular files and symlinks, including directory links, without following links."""
    result = set()
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(app, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                result.add(path.relative_to(app))
            elif not stat.S_ISDIR(mode):
                raise BridgeError("Unsupported file type in change report: " + str(path))
    return result


def same_file(source, output):
    """Compare contents, permissions and link targets, not timestamps or extended attributes."""
    original, generated = source.lstat(), output.lstat()
    if original.st_mode != generated.st_mode:
        return False
    if stat.S_ISLNK(original.st_mode):
        return os.readlink(source) == os.readlink(output)
    if original.st_size != generated.st_size:
        return False
    with source.open("rb") as left, output.open("rb") as right:
        while True:
            chunk = left.read(1024 * 1024)
            if chunk != right.read(1024 * 1024):
                return False
            if not chunk:
                return True


def changed_files(source, output):
    """Compare the whole output bundle, not just the planned replacement roots."""
    originals, generated = bundle_files(source), bundle_files(output)
    added = sorted(generated - originals)
    modified = [relative for relative in sorted(generated & originals)
                if not same_file(source / relative, output / relative)]
    return added, modified


def print_file_changes(source, output, changes):
    say("\n[files] Changes relative to source Xcode 27: " + str(source))
    for label, paths in zip(("ADDED", "MODIFIED"), changes):
        say("%s files (%d):" % (label, len(paths)))
        if not paths:
            say("  (none)")
        for relative in paths:
            say("  [%s] %s" % (label, output / relative))


class Runner:
    def __init__(self, log=None):
        self.log = log

    def __call__(self, arguments, timeout=300, env_overrides=None, allowed_returncodes=(0,)):
        args = [str(item) for item in arguments]
        if self.log:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write("\n$ " + shlex.join(args) + "\n")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}
        environment.update(env_overrides or {})
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True, env=environment)
        try:
            output, _ = child.communicate(timeout=timeout)
        except BaseException:
            # Stop our own child so it cannot keep writing after failure/interruption.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise
        if self.log:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(output)
        if child.returncode not in allowed_returncodes:
            raise BridgeError("Command failed (%s): %s\n%s" % (child.returncode, shlex.join(args), output[-6000:]))
        return output


def copy_artifact(source, destination, run, timeout=300):
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["/usr/bin/ditto", source, destination], timeout=timeout)


def decompress_binary(source, destination, run):
    destination.parent.mkdir(parents=True, exist_ok=True)
    plain = destination.with_name(destination.name + ".plain")
    run(["/bin/cp", "-X", source, plain])
    os.replace(plain, destination)


def sign(path, run, entitlements=False):
    args = ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", "--options", "0"]
    if entitlements:
        args += ["--entitlements", ROOT / "src/entitlements.plist"]
    run(args + [path])


def check_build_tools(target, run):
    """Exercise actual tool processes, including the ibtoold child, not only dlopen."""
    bin_dir = target / "Contents/Developer/usr/bin"
    environment = {"DEVELOPER_DIR": str(target / "Contents/Developer")}
    output = run([bin_dir / "xcodebuild", "-version"], timeout=60, env_overrides=environment).strip()
    if not ("Xcode " + TARGET_VERSION[0] in output and "Build version " + TARGET_VERSION[1] in output):
        raise BridgeError("Unexpected xcodebuild version output: " + output)
    say(output)
    for name in ("ibtool", "actool", "ictool"):
        say("[check] " + name + " --version (also exercises ibtoold)")
        output = run([bin_dir / name, "--version"], timeout=60, env_overrides=environment)
        try:
            # Runner merges stderr with stdout; CoreSimulator can log around XML.
            start, end = output.find("<?xml"), output.rfind("</plist>")
            if start < 0 or end < start:
                raise ValueError("No version plist found")
            data = plistlib.loads(output[start:end + len("</plist>")].encode("utf-8"))
        except (ValueError, plistlib.InvalidFileException, ExpatError) as error:
            raise BridgeError("Invalid %s version output: %s" % (name, output[-2000:])) from error
        # ictool is an actool-mode front end in the pinned Xcode build.
        key = "com.apple.%s.version" % ("actool" if name == "ictool" else name)
        info = data.get(key) if isinstance(data, dict) else None
        if (not isinstance(info, dict) or not info.get("bundle-version") or
                info.get("short-bundle-version") != TARGET_VERSION[0]):
            raise BridgeError("Missing %s version information: %s" % (name, output[-2000:]))
    # These tools do not implement --version. Their normal CLI diagnostics prove
    # startup reached argument parsing; a dyld failure/signal is never accepted.
    for name, arguments, expected in (
        ("momc", (), "usage: momc"),
        ("mapc", (), "usage: mapc"),
        ("intentbuilderc", ("--version",), "invalid option '--version'"),
    ):
        say("[check] " + name + " startup")
        output = run([bin_dir / name] + list(arguments), timeout=60, env_overrides=environment,
                     allowed_returncodes=(1,))
        if expected not in output:
            raise BridgeError("Unexpected %s startup output: %s" % (name, output[-2000:]))
    say("Build-tool version/startup checks passed; this is not a full project build.")


def add_mapping(core):
    metadata = core / "Versions/A/Resources/IDEiOSSupportCore.xcplugindata"
    with metadata.open("rb") as stream:
        data = plistlib.load(stream)
    identifier = "IOS16Lab.iPhoneDeviceLocatorMapping"
    data["plug-in"]["extensions"][identifier] = {
        "id": identifier,
        "point": "Xcode.IDEiOSSupportCore.iOSDeviceLocatorMapping",
        "devicePlatform": "com.apple.platform.iphoneos",
        "class": "DVTiPhone",
        "placeholderClass": "DVTiPhonePlaceholder",
        "priority": "10",
        "deviceClass": [{"classString": name} for name in ("iPhone", "iPod", "iPad", "Watch")],
    }
    with metadata.open("wb") as stream:
        plistlib.dump(data, stream, fmt=plistlib.FMT_BINARY, sort_keys=False)


def compiler_paths(target):
    developer = target / "Contents/Developer"
    clang = developer / "Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
    sdk = developer / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
    if not clang.is_file() or not sdk.is_dir():
        raise BridgeError("Xcode 27 compiler or macOS SDK is missing. Complete official Xcode setup first.")
    return clang, sdk


def stage_device_support(donor, payload, run, device_support=None):
    entries = donor_device_support(donor)
    if device_support is not None and entries != tuple(device_support):
        raise BridgeError("Donor DeviceSupport entries changed during preparation.")
    for relative in entries:
        say("[stage] " + relative)
        source = donor / "Contents" / relative
        expected = fingerprint(source)
        copy_artifact(source, payload / relative, run)
        if fingerprint(payload / relative) != expected:
            raise BridgeError("Staged DeviceSupport copy mismatch: " + relative)


def build_payload(donor, target, payload, run_dir, run, device_support=None):
    """Build and probe staged files only; never modifies either app."""
    contents = target / "Contents"
    clang, sdk = compiler_paths(target)
    flags = [clang, "-arch", "arm64", "-isysroot", sdk, "-mmacosx-version-min=27.0"]
    stage_device_support(donor, payload, run, device_support)
    rpaths = [contents / p for p in ("SharedFrameworks", "Frameworks", "PlugIns", "")]
    link_paths = []
    for path in rpaths:
        link_paths += ["-Xlinker", "-rpath", "-Xlinker", path]
    # Shipped shims use bundle-relative paths, not either source app's location.
    shim_paths = []
    for path in ("@loader_path", "@loader_path/../Frameworks", "@loader_path/../PlugIns", "@loader_path/.."):
        shim_paths += ["-Xlinker", "-rpath", "-Xlinker", path]
    for relative in FRAMEWORKS:
        say("[stage] " + relative)
        source = donor / "Contents" / relative
        destination = payload / relative
        copy_artifact(source, destination, run)
        binary = Path("Versions/A") / Path(relative).stem
        decompress_binary(source / binary, destination / binary, run)
    core = payload / FRAMEWORKS[0]
    core_binary = core / "Versions/A/IDEiOSSupportCore"
    for provider, macro, prefix in (
        ("DVTFoundation", "IOS16_DVTFOUNDATION", "SharedFrameworks"),
        ("IDEFoundation", "IOS16_IDEFOUNDATION", "Frameworks"),
        ("XCTest", "IOS16_XCTEST", "SharedFrameworks"),
        ("DVTMacroFoundation", "IOS16_MACROFOUNDATION", "SharedFrameworks"),
    ):
        say("[build] " + provider + " compatibility shim")
        original_import = "@rpath/%s.framework/Versions/A/%s" % (provider, provider)
        shim_name = "libIOS16%sShim.dylib" % provider
        output = payload / "SharedFrameworks" / shim_name
        output.parent.mkdir(parents=True, exist_ok=True)
        args = flags + ["-dynamiclib", "-D" + macro, ROOT / "src/compatibility.m", "-framework", "Foundation",
                        "-Xlinker", "-reexport_library", "-Xlinker", contents / prefix / (provider + ".framework") / "Versions/A" / provider,
                        "-Wl,-install_name,@rpath/" + shim_name, "-Wl,-compatibility_version,1",
                        "-Wl,-current_version,1", "-Wl,-headerpad_max_install_names"]
        if provider == "DVTMacroFoundation":
            args += ["-Wl,-alias,_OBJC_CLASS_$_IOS16LegacyMacroDefinitionTable,_OBJC_CLASS_$_DVTMacroDefinitionTable"]
        run(args + shim_paths + ["-o", output])
        run(["/usr/bin/install_name_tool", "-change", original_import, "@rpath/" + shim_name, core_binary])
        sign(output, run)
    run(["/usr/bin/install_name_tool", "-change", "@rpath/DVTFoundation.framework/Versions/A/DVTFoundation",
         "@rpath/libIOS16DVTFoundationShim.dylib", payload / FRAMEWORKS[1] / "Versions/A/DTDeviceKitBase"])
    add_mapping(core)
    for relative in FRAMEWORKS:
        sign(payload / relative, run)
    for relative in ENTRIES:
        decompress_binary(contents / relative, payload / relative, run)
        if relative != "MacOS/Xcode":
            sign(payload / relative, run, entitlements=True)
    probe = run_dir / "probe"
    staged_paths = []
    for path in (payload / "SharedFrameworks", payload / "PlugIns"):
        staged_paths += ["-Xlinker", "-rpath", "-Xlinker", path]
    run(flags + [ROOT / "src/probe.m", "-framework", "Foundation", "-lobjc"] + staged_paths +
        link_paths + ["-Wl,-rpath,/usr/lib/swift", "-o", probe])
    sign(probe, run, entitlements=True)
    say("[probe] Loading staged legacy core against native Xcode 27 providers")
    isolated = {"DYLD_FRAMEWORK_PATH": str(payload / "SharedFrameworks") + ":" + str(payload / "PlugIns")}
    say(run([probe, core_binary], timeout=60, env_overrides=isolated).strip())
    installed_probe = run_dir / "probe-installed"
    run(flags + [ROOT / "src/probe.m", "-framework", "Foundation", "-lobjc"] + link_paths +
        ["-Wl,-rpath,/usr/lib/swift", "-o", installed_probe])
    sign(installed_probe, run, entitlements=True)


def check_originals(target, donor):
    for app, relatives in ((target, REQUIRED_ORIGINALS), (donor, FRAMEWORKS + (DEVICE_SUPPORT,))):
        for relative in relatives:
            path = app / "Contents" / relative
            if not path.exists() or path.is_symlink() or not within(path.resolve(), app):
                raise BridgeError("Missing, external or symlinked artifact root: " + str(path))
    for name in ("DeveloperDiskImage.dmg", "DeveloperDiskImage.dmg.signature"):
        image = donor / "Contents" / DEVICE_SUPPORT / "16.4" / name
        if not image.is_file() or image.is_symlink() or not image.stat().st_size:
            raise BridgeError("Donor iOS 16.4 disk image/signature is missing: " + str(image))
    if any(os.path.lexists(target / "Contents" / p) for p in SHIMS):
        raise BridgeError("Source Xcode 27 is already patched. Use an untouched source.")


def preflight(args):
    donor, source = app_path(args.xcode26), app_path(args.xcode27)
    validate_pair(donor, source)
    validate_versions(donor, source)
    compiler_paths(source)
    check_originals(source, donor)
    device_support = donor_device_support(donor)
    validate_target_roots(source, device_support)
    output = output_path(args.output, (donor, source))
    return donor, source, output, device_support


def generate(args):
    host_check(writing=True)
    donor, source, output, device_support = preflight(args)
    assert_not_running(source)
    say("Source Xcode 26: %s\nSource Xcode 27: %s\nNew output: %s" % (donor, source, output))
    say("DeviceSupport entries: " + ", ".join(Path(p).name for p in device_support))
    say("Only the new output will be modified. It will be signed ad hoc and lose Apple's original signature.\n"
        "Account helpers may stop working. No global security changes or device App installation.")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Claim a fresh destination atomically. Never merge into an existing output.
    output.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="xcode-legacy-device-bridge-") as temporary:
            work = Path(temporary).resolve()
            run = Runner(work / "commands.log")
            say("[copy] Creating a full copy of source Xcode 27; this may take several minutes")
            copy_artifact(source, output, run, timeout=7200)
            if app_path(str(output)) != output or version(output) != TARGET_VERSION:
                raise BridgeError("Output copy does not match source Xcode 27.")
            artifacts = FRAMEWORKS + device_support + ENTRIES + SHIMS + ("_CodeSignature",)
            validate_target_roots(output, artifacts)
            payload = work / "payload/Contents"
            build_payload(donor, output, payload, work, run, device_support)
            for relative in artifacts:
                destination = output / "Contents" / relative
                if os.path.lexists(destination):
                    discarded = work / "discarded/Contents" / relative
                    discarded.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, discarded)
                if relative != "_CodeSignature":
                    copy_artifact(payload / relative, destination, run)
                    if fingerprint(destination) != fingerprint(payload / relative):
                        raise BridgeError("Output artifact copy mismatch: " + relative)
            say("[sign] Ad-hoc signing only the new output")
            sign(output, run, entitlements=True)
            run(["/usr/bin/codesign", "--verify", "--strict", "--verbose=1", output])
            run([work / "probe-installed", output / "Contents" / FRAMEWORKS[0] / "Versions/A/IDEiOSSupportCore"], timeout=60)
            check_build_tools(output, run)
            say("[report] Comparing all output files with source Xcode 27; this may take several minutes")
            changes = changed_files(source, output)
    except BaseException:
        say("FAILED: incomplete output retained at %s. Do not open it.\n"
            "Both source apps remain untouched. No rollback is performed; use a new output path to retry." % output)
        raise
    say("Created: %s\nSigning, host-side loading and build-tool startup passed; actual device debugging still needs verification.\n"
        "Open the output manually and approve any security prompt yourself." % output)
    print_file_changes(source, output, changes)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("xcode26", help="Untouched source Xcode 26.6 (17F113) .app")
    result.add_argument("xcode27", help="Untouched source Xcode 27.0 (27A266a) .app")
    result.add_argument("output", help="New output .app path; must not already exist")
    return result


def main():
    if sys.version_info < (3, 9):
        print("ERROR: Python 3.9+ is required.", file=sys.stderr)
        return 1
    args = parser().parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt("Interrupted by signal %s" % signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        generate(args)
        return 0
    except (BridgeError, OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
