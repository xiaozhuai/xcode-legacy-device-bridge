"""Safety/copy tests with synthetic bundles; never executes or modifies real Xcode."""
import argparse
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/bridge.py"
spec = importlib.util.spec_from_file_location("bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class FakeRunner:
    fail_payload_entry = None
    fail_tool = None

    def __init__(self, log=None):
        pass

    def __call__(self, arguments, timeout=300, env_overrides=None, allowed_returncodes=(0,)):
        args = [str(a) for a in arguments]
        if args[0] == "/usr/bin/ditto":
            source, dest = Path(args[1]), Path(args[2])
            if (FakeRunner.fail_payload_entry and "/payload/" in str(source) and
                    str(source).endswith(FakeRunner.fail_payload_entry)):
                FakeRunner.fail_payload_entry = None
                raise bridge.BridgeError("Injected output-copy failure")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, dest)
        elif args[0] == "/usr/bin/codesign" and "--force" in args:
            app = Path(args[-1])
            if app.suffix == ".app":
                signature = app / "Contents/_CodeSignature/CodeResources"
                signature.parent.mkdir(parents=True, exist_ok=True)
                signature.write_text("new seal")
        name = Path(args[0]).name
        if name == FakeRunner.fail_tool:
            raise bridge.BridgeError("Injected tool startup failure: " + name)
        if name == "xcodebuild":
            return "Xcode 27.0\nBuild version 27A266a\n"
        if name in ("ibtool", "actool", "ictool"):
            key = "com.apple.%s.version" % ("actool" if name == "ictool" else name)
            return plistlib.dumps({key: {"bundle-version": "25098", "short-bundle-version": "27.0"}}).decode()
        if name in ("momc", "mapc"):
            if allowed_returncodes != (1,):
                raise AssertionError("CLI usage exit code must be explicitly allowed")
            return "usage: " + name
        if name == "intentbuilderc":
            if allowed_returncodes != (1,):
                raise AssertionError("CLI usage exit code must be explicitly allowed")
            return "error: invalid option '--version'"
        return "synthetic command result"


def fake_build(donor, target, payload, run_dir, runner, device_support=None):
    bridge.stage_device_support(donor, payload, runner, device_support)
    for relative in bridge.FRAMEWORKS + bridge.ENTRIES + bridge.SHIMS:
        path = payload / relative
        if relative.endswith(".framework"):
            path = path / "binary"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("patched " + relative)
    (run_dir / "probe-installed").write_text("synthetic probe")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.make_app("source space,27/Xcode.app", bridge.TARGET_VERSION)
        self.donor = self.make_app("source26/Xcode.app", bridge.DONOR_VERSION)
        self.output = self.root / "new output,27/Xcode.app"
        for app, relatives in ((self.source, bridge.REQUIRED_ORIGINALS), (self.donor, bridge.FRAMEWORKS)):
            for relative in relatives:
                path = app / "Contents" / relative
                if relative.endswith(".framework") or relative == "_CodeSignature":
                    path = path / "binary"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original " + relative)
        for name in ("15.0", "16.0", "16.4"):
            self.make_support(self.donor, name, "donor")
        self.device_support = bridge.donor_device_support(self.donor)
        native = self.source / "Contents/Developer/SDK-marker"
        native.write_text("keep native Xcode 27 components")
        self.source_hashes = self.input_hashes()
        FakeRunner.fail_payload_entry = None
        FakeRunner.fail_tool = None

    def make_support(self, app, name, content):
        path = app / "Contents" / bridge.DEVICE_SUPPORT / name
        path.mkdir(parents=True)
        (path / "DeveloperDiskImage.dmg").write_text(content + " image " + name)
        (path / "DeveloperDiskImage.dmg.signature").write_text(content + " signature " + name)
        return path

    def make_app(self, relative, values):
        path = self.root / relative
        (path / "Contents").mkdir(parents=True)
        with (path / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": "com.apple.dt.Xcode", "CFBundleExecutable": "Xcode"}, stream)
        with (path / "Contents/version.plist").open("wb") as stream:
            plistlib.dump(dict(zip(("CFBundleShortVersionString", "ProductBuildVersion"), values)), stream)
        return path

    def args(self, **overrides):
        values = dict(xcode26=str(self.donor), xcode27=str(self.source), output=str(self.output))
        values.update(overrides)
        return argparse.Namespace(**values)

    def input_hashes(self):
        return {str(p): bridge.fingerprint(p) for p in (self.donor, self.source)}

    @contextlib.contextmanager
    def implementation_mocks(self):
        with contextlib.ExitStack() as stack:
            for name, value in (("host_check", None), ("assert_not_running", None),
                                ("compiler_paths", ("clang", "sdk"))):
                stack.enter_context(mock.patch.object(bridge, name, return_value=value))
            stack.enter_context(mock.patch.object(bridge, "Runner", FakeRunner))
            stack.enter_context(mock.patch.object(bridge, "build_payload", fake_build))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield

    def test_generate_from_source_without_device_support_keeps_sources_unchanged(self):
        self.assertFalse((self.source / "Contents" / bridge.DEVICE_SUPPORT).exists())
        with self.implementation_mocks():
            bridge.generate(self.args())
        self.assertEqual(self.input_hashes(), self.source_hashes)
        support = self.output / "Contents" / bridge.DEVICE_SUPPORT
        self.assertEqual(sorted(p.name for p in support.iterdir()), ["15.0", "16.0", "16.4"])
        for relative in self.device_support:
            self.assertEqual(bridge.fingerprint(self.output / "Contents" / relative),
                             bridge.fingerprint(self.donor / "Contents" / relative))
        for relative in bridge.SHIMS:
            self.assertTrue((self.output / "Contents" / relative).is_file())
        self.assertEqual((self.output / "Contents/Developer/SDK-marker").read_text(),
                         "keep native Xcode 27 components")
        self.assertEqual(sorted(p.name for p in self.output.parent.iterdir()), ["Xcode.app"])

    def test_success_ends_with_a_complete_file_level_change_report(self):
        seal = self.source / "Contents/_CodeSignature"
        (seal / "binary").rename(seal / "CodeResources")
        extra = self.donor / "Contents" / bridge.DEVICE_SUPPORT / "16.4/nested/resource.txt"
        extra.parent.mkdir()
        extra.write_text("nested support resource")
        before = self.input_hashes()
        captured = io.StringIO()
        with self.implementation_mocks(), contextlib.redirect_stdout(captured):
            bridge.generate(self.args())
        self.assertEqual(self.input_hashes(), before)
        text = captured.getvalue()
        report = text[text.index("\n[files] Changes relative to source Xcode 27:"):]
        source_files, output_files = bridge.bundle_files(self.source), bridge.bundle_files(self.output)
        # An independent content-hash comparison checks every printed leaf path.
        expected_added = sorted(output_files - source_files)
        expected_modified = sorted(p for p in output_files & source_files
                                   if bridge.fingerprint(self.output / p) != bridge.fingerprint(self.source / p))
        self.assertEqual([line.strip() for line in report.splitlines() if "[ADDED]" in line],
                         ["[ADDED] " + str(self.output / p) for p in expected_added])
        self.assertEqual([line.strip() for line in report.splitlines() if "[MODIFIED]" in line],
                         ["[MODIFIED] " + str(self.output / p) for p in expected_modified])
        self.assertIn("ADDED files (%d):" % len(expected_added), report)
        self.assertIn("MODIFIED files (%d):" % len(expected_modified), report)
        self.assertIn("[MODIFIED] " + str(self.output / "Contents/_CodeSignature/CodeResources"), report)
        self.assertIn("[ADDED] " + str(self.output / "Contents" / bridge.DEVICE_SUPPORT /
                                     "16.4/nested/resource.txt"), report)
        self.assertNotIn("SDK-marker", report)
        self.assertTrue(text.endswith("[MODIFIED] " + str(self.output / expected_modified[-1]) + "\n"))

    def test_change_report_checks_the_whole_bundle_and_omits_unchanged_files_and_directories(self):
        shutil.copytree(self.source, self.output, symlinks=True)
        unrelated = Path("Contents/Developer/SDK-marker")
        (self.output / unrelated).write_text("modified outside the replacement roots")
        added = Path("Contents/Resources/new folder/nested/file.txt")
        (self.output / added).parent.mkdir(parents=True)
        (self.output / added).write_text("new")
        (self.output / "Contents/empty-directory").mkdir()
        self.assertEqual(bridge.changed_files(self.source, self.output), ([added], [unrelated]))

    def test_change_report_includes_permission_changes_but_ignores_timestamps(self):
        shutil.copytree(self.source, self.output, symlinks=True)
        changed = Path("Contents/Developer/SDK-marker")
        (self.source / changed).chmod(0o644)
        (self.output / changed).chmod(0o755)
        os.utime(self.output / "Contents/Info.plist", (1, 1))
        self.assertEqual(bridge.changed_files(self.source, self.output), ([], [changed]))

    def test_change_report_handles_links_without_following_external_targets(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("do not traverse this directory")
        resources = self.source / "Contents/Resources"
        resources.mkdir()
        (resources / "unchanged link").symlink_to(outside, target_is_directory=True)
        (resources / "changed link").symlink_to("missing-before")
        (resources / "file-to-link").write_text("same path, different type")
        shutil.copytree(self.source, self.output, symlinks=True)
        generated = self.output / "Contents/Resources"
        (generated / "changed link").unlink()
        (generated / "changed link").symlink_to("missing-after")
        (generated / "file-to-link").unlink()
        (generated / "file-to-link").symlink_to(outside, target_is_directory=True)
        (generated / "new link").symlink_to(outside, target_is_directory=True)
        added, modified = bridge.changed_files(self.source, self.output)
        self.assertEqual(added, [Path("Contents/Resources/new link")])
        self.assertEqual(modified, [Path("Contents/Resources/changed link"),
                                    Path("Contents/Resources/file-to-link")])
        self.assertFalse(any("private.txt" in str(p) for p in bridge.bundle_files(self.output)))

    def test_change_report_does_not_read_through_a_source_directory_link(self):
        outside = self.root / "external source directory"
        outside.mkdir()
        (outside / "file.txt").write_text("identical external contents")
        (self.source / "Contents/link").symlink_to(outside, target_is_directory=True)
        shutil.copytree(self.source, self.output, symlinks=True)
        (self.output / "Contents/link").unlink()
        (self.output / "Contents/link").mkdir()
        (self.output / "Contents/link/file.txt").write_text("identical external contents")
        self.assertEqual(bridge.changed_files(self.source, self.output),
                         ([Path("Contents/link/file.txt")], []))

    def test_change_report_compares_all_content_not_only_size_or_timestamps(self):
        shutil.copytree(self.source, self.output, symlinks=True)
        relative = Path("Contents/large-file")
        contents = b"a" * (2 * 1024 * 1024)
        (self.source / relative).write_bytes(contents + b"x")
        (self.output / relative).write_bytes(contents + b"y")
        os.utime(self.source / relative, (1, 1))
        os.utime(self.output / relative, (1, 1))
        self.assertEqual(bridge.changed_files(self.source, self.output), ([], [relative]))

    def test_empty_change_report_has_zero_counts_and_explicit_none(self):
        shutil.copytree(self.source, self.output, symlinks=True)
        changes = bridge.changed_files(self.source, self.output)
        self.assertEqual(changes, ([], []))
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            bridge.print_file_changes(self.source, self.output, changes)
        self.assertTrue(captured.getvalue().endswith("ADDED files (0):\n  (none)\nMODIFIED files (0):\n  (none)\n"))

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir(parents=True)
        marker = self.output / "user-file"
        marker.write_text("keep me")
        with self.implementation_mocks():
            with self.assertRaises(bridge.BridgeError):
                bridge.generate(self.args())
        self.assertEqual(marker.read_text(), "keep me")
        self.assertEqual(self.input_hashes(), self.source_hashes)

    def test_build_tool_launch_failure_prevents_a_successful_generation(self):
        for name in ("ibtool", "actool", "ictool", "momc", "mapc", "intentbuilderc"):
            with self.subTest(tool=name), self.implementation_mocks():
                output = self.root / ("failed-" + name + "/Xcode.app")
                FakeRunner.fail_tool = name
                with self.assertRaises(bridge.BridgeError):
                    bridge.generate(self.args(output=str(output)))
                self.assertTrue(output.is_dir())
                self.assertEqual(self.input_hashes(), self.source_hashes)
        FakeRunner.fail_tool = None

    def test_ibtoold_and_related_tools_are_required_and_staged_for_signing(self):
        for name in ("ibtool", "actool", "ibtoold", "ictool", "momc", "mapc", "intentbuilderc"):
            relative = "Developer/usr/bin/" + name
            self.assertIn(relative, bridge.ENTRIES)
            self.assertIn(relative, bridge.REQUIRED_ORIGINALS)
        helper = self.source / "Contents/Developer/usr/bin/ibtoold"
        helper.unlink()
        with self.implementation_mocks(), self.assertRaises(bridge.BridgeError):
            bridge.preflight(self.args())
        self.assertFalse(self.output.exists())

    def test_version_checks_accept_xml_surrounded_by_logs_and_reject_empty_or_wrong_versions(self):
        normal = FakeRunner()
        def with_logs(arguments, **kwargs):
            return "CoreSimulator diagnostic\n" + normal(arguments, **kwargs) + "\ntrailing diagnostic"
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.check_build_tools(self.source, with_logs)
        for bad in ("", "dyld: Library not loaded", '<?xml version="1.0"?><plist><dict></plist>',
                    plistlib.dumps({"com.apple.ibtool.version": {
                "bundle-version": "old", "short-bundle-version": "26.6"}}).decode()):
            def invalid_version(arguments, **kwargs):
                return bad if Path(arguments[0]).name == "ibtool" else normal(arguments, **kwargs)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(bridge.BridgeError):
                bridge.check_build_tools(self.source, invalid_version)

    def test_runner_accepts_only_explicit_cli_usage_exit_codes_not_crashes(self):
        runner = bridge.Runner()
        with self.assertRaises(bridge.BridgeError):
            runner([sys.executable, "-c", "import sys; sys.exit(1)"])
        runner([sys.executable, "-c", "import sys; sys.exit(1)"], allowed_returncodes=(1,))
        with self.assertRaises(bridge.BridgeError):
            runner([sys.executable, "-c", "import sys; sys.exit(6)"], allowed_returncodes=(1,))

    def test_failure_retains_incomplete_output_without_rollback_or_source_changes(self):
        with self.implementation_mocks():
            FakeRunner.fail_payload_entry = bridge.DEVICE_SUPPORT + "/16.0"
            with self.assertRaises(bridge.BridgeError):
                bridge.generate(self.args())
        self.assertEqual(self.input_hashes(), self.source_hashes)
        self.assertTrue(self.output.is_dir())
        self.assertTrue((self.output / "Contents" / bridge.DEVICE_SUPPORT / "15.0").is_dir())
        self.assertEqual((self.output / "Contents" / bridge.FRAMEWORKS[0] / "binary").read_text(),
                         "patched " + bridge.FRAMEWORKS[0])
        self.assertFalse((self.output.parent / ".Xcode.legacy-device-bridge").exists())

    def test_matching_support_is_replaced_and_source_only_versions_are_retained(self):
        matching = self.make_support(self.source, "16.0", "source27")
        (matching / "source-only-file").write_text("old matching entry")
        extra = self.make_support(self.source, "27.0", "source27")
        before = self.input_hashes()
        with self.implementation_mocks():
            bridge.generate(self.args())
        self.assertEqual(self.input_hashes(), before)
        self.assertFalse((self.output / "Contents" / bridge.DEVICE_SUPPORT / "16.0/source-only-file").exists())
        self.assertEqual(bridge.fingerprint(self.output / "Contents" / bridge.DEVICE_SUPPORT / "27.0"),
                         bridge.fingerprint(extra))

    def test_stage_copies_all_contents_including_non_version_entries(self):
        root = self.donor / "Contents" / bridge.DEVICE_SUPPORT
        (root / "README.txt").write_text("donor metadata")
        extras = root / "15.0/nested"
        extras.mkdir()
        (extras / "extra.txt").write_text("extra support data")
        payload = self.root / "staged/Contents"
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.stage_device_support(self.donor, payload, FakeRunner())
        self.assertEqual(bridge.fingerprint(root), bridge.fingerprint(payload / bridge.DEVICE_SUPPORT))

    def test_device_support_plan_changes_abort_before_staging(self):
        self.make_support(self.donor, "15.2", "later donor")
        payload = self.root / "staged/Contents"
        with self.assertRaises(bridge.BridgeError):
            bridge.stage_device_support(self.donor, payload, FakeRunner(), self.device_support)
        self.assertFalse(payload.exists())

    def test_production_output_and_symlink_alias_are_rejected(self):
        production = self.root / "Applications"
        production.mkdir()
        alias = self.root / "output-alias"
        alias.symlink_to(production, target_is_directory=True)
        for path in (production / "New.app", alias / "New.app", Path("/System/New.app"), Path("/Library/New.app")):
            with self.assertRaises(bridge.BridgeError):
                bridge.output_path(str(path), (self.donor, self.source))

    def test_source_under_applications_is_allowed_as_read_only_input(self):
        source = self.make_app("Applications/Xcode.app", bridge.TARGET_VERSION)
        self.assertEqual(bridge.app_path(str(source)), source)
        self.assertEqual(bridge.output_path(str(self.output), (self.donor, source)), self.output)

    def test_output_cannot_be_source_or_nested_inside_source(self):
        for path in (self.source, self.donor, self.source / "Nested.app", self.donor / "Nested.app"):
            with self.assertRaises(bridge.BridgeError):
                bridge.output_path(str(path), (self.donor, self.source))

    def test_existing_or_dangling_output_symlinks_are_rejected(self):
        alias = self.root / "Alias.app"
        alias.symlink_to(self.source, target_is_directory=True)
        dangling = self.root / "Dangling.app"
        dangling.symlink_to(self.root / "missing.app", target_is_directory=True)
        for path in (alias, dangling):
            with self.assertRaises(bridge.BridgeError):
                bridge.output_path(str(path), (self.donor, self.source))

    def test_artifact_parent_cannot_escape_source_bundle(self):
        outside = self.root / "outside"
        outside.mkdir()
        frameworks = self.source / "Contents/SharedFrameworks"
        frameworks.rename(outside / "frameworks")
        frameworks.symlink_to(outside / "frameworks", target_is_directory=True)
        with self.implementation_mocks():
            with self.assertRaises(bridge.BridgeError):
                bridge.preflight(self.args())
        self.assertFalse(self.output.exists())

    def test_donor_device_support_symlink_and_empty_root_are_rejected(self):
        support = self.donor / "Contents" / bridge.DEVICE_SUPPORT
        (support / "alias").symlink_to(support / "16.4", target_is_directory=True)
        with self.assertRaises(bridge.BridgeError):
            bridge.donor_device_support(self.donor)
        empty = self.make_app("empty-donor/Xcode.app", bridge.DONOR_VERSION)
        (empty / "Contents" / bridge.DEVICE_SUPPORT).mkdir(parents=True)
        with self.assertRaises(bridge.BridgeError):
            bridge.donor_device_support(empty)

    def test_already_patched_source_is_rejected(self):
        shim = self.source / "Contents" / bridge.SHIMS[0]
        shim.write_text("existing patch")
        with self.implementation_mocks():
            with self.assertRaises(bridge.BridgeError):
                bridge.generate(self.args())
        self.assertFalse(self.output.exists())

    def test_same_or_nested_sources_are_rejected(self):
        for donor in (self.source, self.source / "nested.app", self.source.parent):
            with self.assertRaises(bridge.BridgeError):
                bridge.validate_pair(donor, self.source)

    def test_unknown_version_is_rejected(self):
        donor = self.make_app("wrong/Xcode.app", ("26.5", "wrong"))
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_versions(donor, self.source)

    def test_cli_accepts_exactly_three_positional_paths(self):
        values = [str(self.donor), str(self.source), str(self.output)]
        args = bridge.parser().parse_args(values)
        self.assertEqual(vars(args), vars(self.args()))
        for invalid in (values[:2], values + ["extra"], ["restore", "--target", str(self.source)]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bridge.parser().parse_args(invalid)

    def test_symlink_fingerprints_do_not_follow_external_files(self):
        external = self.root / "outside-file"
        external.write_text("a")
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "link").symlink_to(external)
        first = bridge.fingerprint(tree)
        external.write_text("b")
        self.assertEqual(first, bridge.fingerprint(tree))


if __name__ == "__main__":
    unittest.main()
