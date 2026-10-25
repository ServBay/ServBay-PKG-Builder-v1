# Windows v2 PHP core 与第三方模块

本文件记录 2026-10-25 在 Windows 11 VM 上的真实 PHP v2 打包结果。所有 series 都使用 PHP-Windows-Portable 对应 Actions run 的原始 core/扩展产物，不从 v1 merged 包逆向拆分，也不从路径字符串推断 ABI。

## 已验证的 series

统一 producer：PHP-Windows-Portable，commit `5f5af83f797ae89a7187ddca87492cf8064d68d7`，全部 `run_attempt=1`。

| PHP | Actions run | core runtime ZIP | 工具链 | ABI（module / zend） | build 标识 | core 7z SHA256 | 模块数 | Uploader 包数 |
|---|---|---:|---|---|---|---|---:|---:|
| 8.2.35 | 39616553240 | `php-8.2.35-nts-Win32-vs16-x64.zip` | msvc-vs16 | 20220829 / 420220829 | `API20220829,NTS,VS16` | `3a5df960887c85e8b5bbc5dfff57ed03f7c3076779abb0270f3c3b32dbe71a85` | 16 | 17 |
| 8.3.36 | 39616551856 | `php-8.3.36-nts-Win32-vs16-x64.zip` | msvc-vs16 | 20230831 / 420230831 | `API20230831,NTS,VS16` | `5669f7325f139cb6a078cb4844b637ec8a5841917ddbfe73ed6cd50db0a93c09` | 16 | 17 |
| 8.4.27 | 39616547511 | `php-8.4.27-nts-Win32-vs17-x64.zip` | msvc-vs17 | 20240924 / 420240924 | `API20240924,NTS,VS17` | `41437cf0b61b073cd320116914ba1ab01eddec83fad6109d6da7f848c7a33b51` | 17 | 18 |
| 8.5.12 | 39616547329 | `php-8.5.12-nts-Win32-vs17-x64.zip` | msvc-vs17 | 20250925 / 420250925 | `API20250925,NTS,VS17` | `0246497535f195b2735941e959500a442e12c0e2b08bf9a5f03173e6800be98d` | 17 | 18 |
| 8.6.0beta3 | 34275585379 | `php-8.6.0beta3-nts-Win32-vs18-x64.zip` | msvc-vs18 | 20250926 / 420250926 | `API20250926,NTS,VS18` | `a76ca7f0488c4d472b39ae07b7847908f0293b43bdb21c11351145cd5c08b909` | 16 | 17 |

全部为 x64、NTS、非 Debug。`extensions.default.json` 同时列出 `imap` 与 `phalcon`，但实际选择以该 run 的扩展分组为准：8.2/8.3 的 run 未产出 `imap` 分组，8.6beta3 的 run 未产出 `phalcon` 分组。这是 producer 资产的真实差异，不是过滤或遗漏。

模块集合：

- 8.2 / 8.3（16）：`apcu grpc igbinary imagick memcache memcached mongodb msgpack oauth pdo_sqlsrv phalcon protobuf redis sqlsrv xdebug yaml`
- 8.4 / 8.5（17）：上述集合 + `imap`
- 8.6.0beta3（16）：`imap` 在列，无 `phalcon`

版本号以 producer 请求为记录（apcu 5.1.28、grpc 1.78.0、igbinary 3.2.16、imagick 3.8.1、imap 1.0.3、memcache 8.2、memcached 3.4.0、mongodb 2.2.1、msgpack msgpack-3.0.0、oauth 2.0.10、pdo_sqlsrv/sqlsrv 5.12.0、phalcon 5.10.0、protobuf 4.29.6、redis 6.3.0、xdebug 3.5.0、yaml 2.3.0）。实际加载验证只证明 DLL 可加载，不把请求版本当作加载后自报版本。

## 每个 series 的实际验证

1. 收集器从 core artifact 与逐扩展 artifact 生成 `split-assets.json`；repacker 校验索引和每个 ZIP 的成员/CRC/命名空间，拒绝 merged、no-extensions、devel/debug/source/test 作为 runtime 输入。
2. 生成 1 个 core 7z 与 N 个模块 7z，并对输出做完整回读（文件集合与逐文件 SHA）。
3. 在解包后的 v2 core 树上叠加全部模块，用 `php.exe -n -d extension_dir=...` 逐个加载：
   - 8.2.35：16/16 `LOADED`
   - 8.3.36：16/16 `LOADED`
   - 8.4.27：17/17 `LOADED`
   - 8.5.12：17/17 `LOADED`
   - 8.6.0beta3：16/16 `LOADED`
   - 每个 series 的 Imagick 都额外执行 PNG formats 查询并返回 `OK`。
4. 以原生结果生成正式 schema-2 core/module 包。每个模块 `host.abi` 与该 series 的实际 core 一致，`host.package_sha256` 等于上表 core SHA。
5. Uploader 的 `build_windows_publish_plan()` 读取全部正式 meta 与相邻归档，五个 series 均为 `validation_complete=true`，顺序为**全部模块在前、PHP core 最后**，以保留 core `default_modules` 对模块 SHA 的引用。

PHP 内置扩展仍由 core 所有；独立模块只包含第三方扩展及其依赖 DLL、资源和配置模板。

## 8.6beta3 的运行时前置

PHP 8.6 由 MSVC 14.51（VS2026，vs18）构建，`php.exe` 的导入表为 `VCRUNTIME140.dll`。测试 VM 原只有 14.44 运行时，启动被 PHP 明确拒绝：

```text
PHP Warning: 'C:\WINDOWS\SYSTEM32\VCRUNTIME140.dll' 14.44 is not compatible with this PHP build linked with 14.51
```

安装 Microsoft `VC_redist.x64.exe`（VS2026 通道，`14.51.36247`）后正常启动并完成全部验证。**这是 8.6 包在客户端上的前置条件**：只安装 2015-2022 运行时的机器无法运行该 core。8.2-8.5 不受影响（vs16/vs17 构建）。

## Legacy 分支（5.5–8.1）

EOL 分支由 Builder 的 `build-windows-php-legacy.yml` / `runtime-windows/ci/build_php_legacy.ps1` 从源码编译。2026-10-25 起该 workflow 在保留原 merged 7z 的同时，额外输出 v2 所需的 core runtime ZIP、devel pack 和逐扩展 runtime ZIP，并以独立 artifact 上传。拆分依据是构建阶段记录的 PECL 文件归属；PECL 文件如果与 core 文件同名但字节不同，构建直接失败，不会用覆盖顺序决定结果。

所有构建均为 `run_attempt=1`，producer 为 `ServBay/ServBay-PKG-Builder-v1`。除 5.5 外均使用 commit `cebe6fad829ee9c32c2cffaea5d79779a8e09ae0`；5.5 使用 `4cd3d36db2042d087c8543f4008aebf3ba564a89`（允许无扩展的 core-only 分支）。

| PHP | Actions run | 工具链 | ABI module/zend | core 7z SHA256 | 模块数 | 正式包数 | 正式加载 | PNG | Uploader |
|---|---:|---|---|---|---:|---:|---|---|---|
| 5.5.38.1 | 40186255646 | msvc-VC11 | 20121113 / 220121212 | `8f3ead4a89fba0f79b3aa668635f03bd5dd5da365082d53edd41233d4492ecad` | 0 | 1 | — | — | true |
| 5.6.40.1 | 40185343226 | msvc-VC11 | 20131106 / 220131226 | `fa25353b5fea6b1259c5a0585ec5089df04656c7128bd9d99b7bfbab8657c052` | 10 | 11 | 10/10 | 无 coder | true |
| 7.0.33.1 | 40185370775 | msvc-VC14 | 20151012 / 320151012 | `4279ab30bacb349490bd0787c9d9219ec505849a752d6d4942d938983adf7c7a` | 12 | 13 | 12/12 | 无 coder | true |
| 7.1.33.1 | 40185365668 | msvc-VC14 | 20160303 / 320160303 | `be8829355e80f208672e19a09bf6e584e7377238c5b1ffc36f078624bcee91a7` | 12 | 13 | 12/12 | OK | true |
| 7.2.34.1 | 40185360220 | msvc-VC15 | 20170718 / 320170718 | `c78bfb03c1c345706b14c4e5ef902ebe285e2102e2f5753f51aba5e3d2f438d1` | 15 | 16 | 15/15 | OK | true |
| 7.3.33.1 | 40185353934 | msvc-VC15 | 20180731 / 320180731 | `b2a23bd8f2026ab8915166597da1847ed62e0ae802055eb243ee071c45b964f0` | 15 | 16 | 15/15 | OK | true |
| 7.4.33.1 | 40185332609 | msvc-vc15 | 20190902 / 320190902 | `daa5fab61b395f71daf2f91a4d46ddc63d6f45731a7aa24692552e16bd9441bf` | 15 | 16 | 15/15 | OK | true |
| 8.0.30.1 | 40185348582 | msvc-vs16 | 20200930 / 420200930 | `31844a9bc4a519ba741a6e46cd61e3111f3e4b9ee7f3bdbb0a9a392e5a9d4d1a` | 15 | 16 | 15/15 | OK | true |
| 8.1.34.1 | 40185337982 | msvc-vs16 | 20210902 / 420210902 | `0dcc218c60e5d00d7fbe4303dde54d3e8f9e9eabf086dd0f3ea3a1d0d27efb39` | 13 | 14 | 13/13 | OK | true |

说明：

- 每个版本的第三方扩展集合由该分支的 `php-exts.json` 过滤结果决定，不跨版本推断。5.5 从未发布 PECL 扩展，因此是 core-only 包（1 个正式包，Uploader 只排 core）。
- devel pack 使用上游对应 base 版本的官方包（例如 7.4.33.1 使用 7.4.33 的 devel pack），重命名后绑定到本地安全版本；安全补丁不改变用于编译扩展的头文件。`split-manifest.json` 同时记录上游文件名和实际 SHA。
- imagick 在 5.6 与 7.0 被钉在 3.4.3，该版本不带 `IM_MOD_RL_*` coder，PNG 转换依赖外部 ImageMagick。原生证据把 `imagick_png` 记为 false，finalize 只有在该模块确实没有 coder 时才接受；不能让缺 coder 的包伪装成通过。
- 5.6/5.5 的 CRT 为 vc11-runtime，构建会把 `msvcr110.dll`/`msvcp110.dll` 一并打包；7.x/8.x 的 vc14-runtime 依赖系统 VC++ 2015-2022 运行时（ServBay 既有前置条件）。

### 无法构建的两个分支

`5.3.29`（run `40185387711`）和 `5.4.45`（run `40185382316`）在本轮 CI 中失败，证据如下：

- 上游 `windows.php.net` 从未提供 5.3/5.4 的 x64 官方包（本次 HEAD 返回 404），而 legacy 脚本以官方 archive 为基底。
- 即使忽略基底，VC9 回退工具链（MSVC 14.29）在链接 `php5.dll` 时产生 1601 个未解析符号（`UTF8ToHtml`、`__xmlDefaultSAXHandler`、`zError`、`zlibVersion` 等），说明 vc9 x64 依赖库与当前工具链不可用。

因此 5.3/5.4 的 v2 打包仍保持未支持；v1 包继续由既有流程提供。没有用当前无法验证的二进制冒充新构建。

## 未覆盖与边界

- v1 API 中 `php` 共 16 个版本：8.2–8.6 由 PHP-Windows-Portable split producer 覆盖，5.5–8.1 由 legacy producer 覆盖；5.3/5.4 因上游 x64 基底缺失与 VC9 链接失败保持未支持。
- 以上验证是打包与 DLL 加载验证，不等同于客户端安装、Web 站点请求、php.ini 生效或卸载验收；这部分仍属于阶段 4 的客户端原生验收。
- legacy split artifact 目前只保存在 Actions artifact（30 天），尚未写入 Release 永久资产；生产发布前必须把 core/扩展资产与 SHA 落到 Release。
