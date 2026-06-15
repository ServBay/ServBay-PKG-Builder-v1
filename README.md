# ServBay Package Build

ServBay 软件包的编译脚本，通过 GitHub Actions 在托管 runner 上构建。

## 结构
- `runtime/` — macOS 软件包编译（`build_package`：从源码编译 / 下载二进制）
- `runtime-windows/` — Windows 软件包打包（`build_windows`：下载 + 7z）
- `.github/workflows/` — 构建流程

## 本地用法
```sh
# macOS
cd runtime && ./build_package -a <x86_64|arm64> -p <name>-<version>

# Windows（在 Linux/macOS 上运行，纯下载 + 7z）
cd runtime-windows && ./build_windows <x64|x86|arm64> -p <name>-<version>
```
