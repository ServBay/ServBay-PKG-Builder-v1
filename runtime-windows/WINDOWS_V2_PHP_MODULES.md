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

## 未覆盖与边界

- **PHP 5.3-8.1（11 个 v1 版本）未做 v2 打包。** 这些版本由 Builder 的 `build-windows-php-legacy.yml` / `runtime-windows/ci/build_php_legacy.ps1` 自行编译，workflow 只上传 merged `php-<version>-windows-<arch>.7z` 与 `meta.json`，没有 core/逐模块 artifact。v2 要求 core 与第三方扩展独立打包，因此当前 producer 输出无法作为 v2 输入；不能从 merged 包按 `php_*.dll` 猜分第三方扩展。要覆盖这些版本，legacy workflow 必须像 PHP-Windows-Portable 一样输出 core runtime 与逐扩展产物（构建脚本已维护 `$PeclExts` 名单，具备拆分信息）。
- v1 API 中 `php` 共 16 个版本；本次覆盖其中 8.2-8.6 共 5 个版本，均由 split producer 产出。其余 11 个版本的缺口即上一条。
- 以上验证是打包与 DLL 加载验证，不等同于客户端安装、Web 站点请求、php.ini 生效或卸载验收；这部分仍属于阶段 4 的客户端原生验收。
