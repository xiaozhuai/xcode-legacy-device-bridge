# xcode-legacy-device-bridge

[English](README.md) | [简体中文](README_CN.md)

An experimental script that creates a new Xcode 27 copy with Xcode 26.6's legacy device pipeline, intended to restore basic debugging on physical iOS 16 devices.

**Not supported by Apple, and not a universal patch.** Only the version combination below is accepted. There is no `--force` option to bypass checks for unknown versions. 

**It's not perfect right now, and using it at your own risk.**

| Component | Requirement |
| --- | --- |
| Host | Apple Silicon, macOS 27 (the original device experiment used 27.0.1) |
| Source Xcode 26 | Untouched Xcode (Apple Silicon) 26.6, build `17F113` |
| Source Xcode 27 | Untouched Xcode 27.0, build `27A266a` |
| DeviceSupport | All entries are copied from Xcode 26 |
| Output | A new `.app` path that does not already exist, outside Applications/System/Library |
| Dependencies | Python 3.9+, Xcode 27's clang/macOS SDK, and built-in macOS tools |

This repository contains no Apple frameworks, disk images, development certificates, provisioning profiles, Apple accounts, or device identifiers. Obtain Xcode through official channels and complete its normal installation and license setup yourself. The script does not download these files.

You can download Xcode from here [https://xcodereleases.com/](https://xcodereleases.com/)

## One-command generation

The script takes exactly three positional paths: **source Xcode 26, source Xcode 27, new output Xcode 27**.

```bash
./bridge.sh \
  "/path/to/xcode26.6/Xcode.app" \
  "/path/to/xcode27/Xcode.app" \
  "/path/to/output/Xcode.app"
```

Quit source Xcode 27 and its build/debug processes first. Do not use `sudo`. Paths may contain spaces. Source apps may be in `/Applications` because they are only read; the output cannot be in a production/system location or inside either source app, including through symlinks. Existing output paths are never overwritten, and already-patched source Xcode 27 copies are rejected.

The script checks versions and paths, creates a full copy of source Xcode 27, compiles compatibility shims in a temporary directory, checks staged loading, copies legacy frameworks and all donor DeviceSupport entries into the output, re-signs only the output, and checks its signature, host-side loading, and build-tool startup. Allow sufficient disk space for a full Xcode copy and temporary build files; the initial copy can take several minutes. It does not change `xcode-select`.

At the end of a successful run, the script compares the entire output bundle with source Xcode 27 and prints every added or modified file's absolute output path, grouped as `[ADDED]` and `[MODIFIED]` with counts. This includes individual framework resources, DeviceSupport files, signature files, and symlinks, not just component directories. Unchanged cloned files are omitted. Comparison checks file contents, permissions, and symlink targets; directories, timestamps, extended attributes, and removed files are not reported. This final read-only comparison can take several minutes.

Every entry under Xcode 26's `Contents/Developer/Platforms/iPhoneOS.platform/DeviceSupport/` is copied with its complete contents and original disk-image signatures, not just version 16.4. A clean Xcode 27 can have no DeviceSupport directory at all; the script creates it in the output. If a source Xcode 27 does contain entries, matching entries in the output are replaced with donor entries, while Xcode-27-only entries are retained. Copying more versions does not establish debugging compatibility with those iOS versions.

There are no `install`, `restore`, or `doctor` subcommands, backup manifests, or rollback operations. If generation fails or is interrupted, both sources remain untouched and the incomplete output is retained. Do not launch that output; remove it yourself or choose a different output path before retrying.

## First launch and device verification

Successful generation confirms signing, host-side loading, and basic build-tool startup. It does not establish that your device can be debugged or that every project can be built.

1. Open the generated output manually. If Gatekeeper blocks it, review and approve only this experimental copy yourself. The script neither removes quarantine nor disables global security checks.
2. If the External Agent Access setup appears, you can select Never; external agent access is not needed for this test. An ad-hoc-signed copy may show this page on every launch.
3. Connect, trust, and unlock the device, and enable Developer Mode through the normal iOS workflow.
4. Select the physical iOS 16 device and run a small test project of your own. Verify installation, launch, source breakpoints, variable inspection, stepping, and continuing execution.

An `xcdevice` status of available or a BUILD SUCCEEDED message alone does not prove the complete pipeline has been restored.

## Permissions and signing limitations

The script ad-hoc signs the **new output** and modified components, using the public `get-task-allow` and `disable-library-validation` entitlements. The latter allows this experimental app to load compatibility libraries; it does not disable global SIP or Gatekeeper. The output no longer has Apple's original signature or the private privileges required by account helper services.

- If macOS denies writes to the app bundle, check App Management permission in System Settings for the terminal running the script. Do not use `sudo` as a way to bypass TCC. Retry with a new output path.
- Apple Accounts may report "Couldn't communicate with a helper application". Sign in and prepare signing/provisioning profiles in an unmodified production Xcode. The experimental output can use existing certificates and local provisioning profiles.
- The script does not add Apple-private entitlements, extract credentials, relax Keychain ACLs, register App IDs, create certificates, or install a test app on a device.
- Inside the new copy, it replaces three legacy frameworks, prepares/re-signs ten entry-point binaries (`Xcode`, `xcodebuild`, `xcdevice`, `ibtool`, `actool`, `ibtoold`, `ictool`, `momc`, `mapc`, and `intentbuilderc`), adds four shims and all donor DeviceSupport entries, and regenerates the top-level signature. Xcode 27's SDKs, compiler, IDEFoundation, DVTFoundation, DVTMacroFoundation, and LLDB are retained. Both source apps are read-only inputs.

`ibtool`/`actool`/`ictool` use the independent `ibtoold` process, which also loads the transplanted framework. Signing only the Xcode app or only a front-end tool is insufficient. The generated copy therefore re-signs those tools and the related model compilers with the public entitlements above. Generation checks their actual version/startup output and rejects loader failures; it does not merely check `xcodebuild -version`.
