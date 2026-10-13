# xcode-legacy-device-bridge

[English](README.md) | [简体中文](README_CN.md)

实验脚本：以 Xcode 27 为基础生成一份新副本，接入 Xcode 26.6 的旧设备链路，以恢复 iOS 16 真机的基本调试功能。

**非 Apple 官方支持，也不是通用补丁。** 当前只接受下面这个版本组合，没有绕过未知版本检查的 `--force` 参数。

**目前还并不完美，使用它需要自行承担风险**

| 项目 | 条件 |
| --- | --- |
| 主机 | Apple Silicon、macOS 27（原始真机实验使用 27.0.1） |
| 源 Xcode 26 | 未修改的 Xcode 26.6，build `17F113` |
| 源 Xcode 27 | 未修改的 Xcode 27.0，build `27A266a` |
| DeviceSupport | 从 Xcode 26 复制所有项 |
| 输出 | 尚不存在的新 `.app` 路径，位于 Applications/System/Library 之外 |
| 依赖 | Python 3.9+、Xcode 27 的 clang/macOS SDK、macOS 自带工具 |

本仓库不包含任何 Apple 框架、磁盘镜像、开发证书、描述文件、Apple 账号或设备标识。请自行从官方渠道获得 Xcode，完成其正常安装/许可流程。脚本不联网下载这些文件。

你可以在这里下载原版Xcode [https://xcodereleases.com/](https://xcodereleases.com/)

## 一键生成

脚本接受且只接受三个位置参数：**源 Xcode 26、源 Xcode 27、输出的新 Xcode 27**。

```bash
./bridge.sh \
  "/path/to/xcode26.6/Xcode.app" \
  "/path/to/xcode27/Xcode.app" \
  "/path/to/output/Xcode.app"
```

先退出源 Xcode 27 及其构建/调试进程，不要使用 `sudo`。路径可以包含空格。源 App 可以位于 `/Applications`，因为只会读取；输出不能位于正式安装/系统位置，也不能位于任一源 App 内，符号链接同样会检查。已有输出不会被覆盖，已打补丁的源 Xcode 27 也会被拒绝。

脚本依次完成版本/路径检查、完整复制源 Xcode 27、在临时目录编译兼容层、暂存加载检查、向输出移植旧框架和供体全部 DeviceSupport、只对输出重签名，以及输出签名/主机加载/构建工具启动检查。请预留一份完整 Xcode 副本及临时构建文件所需的空间；初次复制可能耗时数分钟。不会更改 `xcode-select`。

成功结束时，脚本会对比整个输出 App 与源 Xcode 27，按 `[ADDED]`（新增）和 `[MODIFIED]`（修改）分类打印每个文件的绝对输出路径及数量。清单包括框架内部资源、DeviceSupport、签名文件和符号链接，不只是组件目录；完整复制但未变化的文件不会列入。比较文件内容、权限和符号链接目标，不统计目录、时间戳、扩展属性或已删除文件。最后这一步只读比较可能耗时数分钟。

Xcode 26 的 `Contents/Developer/Platforms/iPhoneOS.platform/DeviceSupport/` 下所有项及其完整内容、原始磁盘镜像签名都会复制，不再只复制 16.4。干净的 Xcode 27 可以完全没有 DeviceSupport，脚本会在输出中创建它。如果源 27 已有该目录，输出中的同名项以供体为准，源 27 独有项保留。复制更多版本不代表已验证相应 iOS 版本的调试兼容性。

不提供 `install`、`restore`、`doctor` 子命令，也不创建备份清单或执行回滚。失败或中断时，两份源保持不变，不完整的输出会保留。不要打开该输出；请自行移除它，或换一个输出路径后重试。

## 首次启动与真机验证

生成成功只说明签名、主机侧加载及构建工具基本启动通过，不代表你的设备已经可以调试，也不代表所有项目都能构建。

1. 手动打开生成的输出。如果 Gatekeeper 拦截，由你自己审查并只批准这份实验副本；脚本不会移除 quarantine，也不会关闭全局安全检查。
2. 若出现 External Agent Access 设置，测试不需要开放外部 Agent，可选 Never。ad-hoc 副本可能每次启动重复显示此页。
3. 连接、信任并解锁设备，按 iOS 正常流程启用 Developer Mode。
4. 选中 iOS 16 真机运行你自己的小型测试项目，确认安装、启动、源码断点、读取变量、单步和继续运行。

不要只以 `xcdevice` 的 available 状态或 BUILD SUCCEEDED 判断完整链路已恢复。

## 权限和签名限制

脚本会给**新输出**及修改过的组件做 ad-hoc 签名，并使用公开的 `get-task-allow` 和 `disable-library-validation` entitlement；后者仅允许这份实验 App 加载兼容库，不是关闭全局 SIP/Gatekeeper。输出不再具有 Apple 原始签名和账号辅助程序所需的私有权限。

- 若 macOS 报 App Bundle 写入被拒绝，在系统设置中为运行脚本的终端检查 App Management 权限。`sudo` 不应当作绕过 TCC 的办法。重试时使用新的输出路径。
- Apple Accounts 可能报 “Couldn't communicate with a helper application”。在未修改的正式 Xcode 中登录并准备签名/描述文件；实验输出可使用已有证书和本地描述文件。
- 脚本不会添加 Apple 私有 entitlement、提取凭据、放宽钥匙串 ACL、注册 App ID、创建证书或在设备上安装测试 App。
- 只在新副本中替换三个旧框架，准备/重签十个入口二进制（`Xcode`、`xcodebuild`、`xcdevice`、`ibtool`、`actool`、`ibtoold`、`ictool`、`momc`、`mapc`、`intentbuilderc`），加入四个 shim 和供体全部 DeviceSupport，并重新生成顶层签名。27 的 SDK、编译器、IDEFoundation、DVTFoundation、DVTMacroFoundation 和 LLDB 保留。两份源 App 只读。

`ibtool`/`actool`/`ictool` 使用独立的 `ibtoold` 子进程，该进程同样会加载移植框架。仅重签 Xcode 主 App 或仅重签前端工具还不够，因此输出会同时重签这些工具及相关模型编译器，使用上面列出的公开 entitlement。生成完成前会验证实际版本/启动输出，拒绝加载失败，不再只检查 `xcodebuild -version`。
