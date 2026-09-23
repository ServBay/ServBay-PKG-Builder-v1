import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuildScriptTests(unittest.TestCase):
    def test_macos_git_build_disables_optional_rust_components(self):
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")
        build_git = self._extract_shell_function(script, "build_git")

        self.assertIn("NO_RUST=1", build_git)
        self.assertRegex(build_git, r"make\s+-j\$\{CPU_NUMBER\}[^\\n]*NO_RUST=1")
        self.assertRegex(build_git, r"make\s+install[^\\n]*NO_RUST=1")

    def test_windows_git_uses_portable_git_with_shell_tools(self):
        script = (ROOT / "runtime-windows" / "build_windows").read_text(encoding="utf-8")
        build_git = self._extract_shell_function(script, "Build_Package_git")

        self.assertIn("PortableGit-${package_version}-${git_arch_bit}.7z.exe", build_git)
        self.assertNotIn("MinGit-${package_version}", build_git)
        self.assertIn('arm64) git_arch_bit="arm64"', build_git)
        self.assertIn('"git-${package_version}"', build_git)

    def test_macos_ollama_builds_and_copies_the_complete_native_payload(self):
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")
        build_ollama = self._extract_shell_function(script, "build_ollama")

        self.assertIn('[[ -f "CMakeLists.txt"', build_ollama)
        self.assertIn('-f "llama/server/CMakeLists.txt"', build_ollama)
        self.assertRegex(build_ollama, r'x86_64\)\s+ollama_arch="amd64"')
        self.assertRegex(build_ollama, r'arm64\)\s+ollama_arch="arm64"')
        self.assertNotIn("build_darwin.sh", build_ollama)
        self.assertIn('-DOLLAMA_MLX_BACKENDS=metal_v3', build_ollama)
        self.assertNotIn("metal_v4", build_ollama)
        self.assertIn('-DCMAKE_OSX_ARCHITECTURES=${cmake_arch}', build_ollama)
        self.assertIn('local macos_min="14.0"', build_ollama)
        self.assertIn('-DCMAKE_OSX_DEPLOYMENT_TARGET=${macos_min}', build_ollama)
        self.assertIn('-DCMAKE_INSTALL_PREFIX=${payload_dir}/', build_ollama)
        self.assertIn('-DOLLAMA_PAYLOAD_INSTALL_PREFIX=${payload_dir}/', build_ollama)
        self.assertIn('-DOLLAMA_GO_OUTPUT=${payload_dir}/ollama', build_ollama)
        self.assertIn('-DOLLAMA_VERSION=${version}-ServBay', build_ollama)
        self.assertNotIn('-DOLLAMA_VERSION=${version} (ServBay)', build_ollama)
        self.assertIn('-DOLLAMA_LLAMA_BACKENDS=', build_ollama)
        self.assertIn('-DMLX_ENABLE_X64_MAC=ON', build_ollama)
        self.assertIn('-ldl -lc++ -framework Accelerate', build_ollama)
        self.assertIn(
            '-lc++ -framework Metal -framework Foundation -framework Accelerate',
            build_ollama,
        )
        self.assertIn('FETCHCONTENT_SOURCE_DIR_LLAMA_CPP=', build_ollama)
        self.assertIn('FETCHCONTENT_SOURCE_DIR_MLX=', build_ollama)
        self.assertIn('FETCHCONTENT_SOURCE_DIR_MLX-C=', build_ollama)
        self.assertIn("ollama-llama-cpp-source", build_ollama)
        self.assertIn("ollama-mlx-sources", build_ollama)
        self.assertIn("ollama-local", build_ollama)
        self.assertIn("ollama-mlx-backends", build_ollama)
        self.assertIn('GOOS=darwin GOARCH="$ollama_arch" CGO_ENABLED=1', build_ollama)
        self.assertIn('CGO_CFLAGS="$mlx_cgo_cflags"', build_ollama)
        self.assertIn('CGO_CXXFLAGS="$mlx_cgo_cxxflags"', build_ollama)
        self.assertIn('CGO_LDFLAGS="$mlx_cgo_ldflags"', build_ollama)
        self.assertIn('lipo "$payload" -verify_arch "$cmake_arch"', build_ollama)
        self.assertIn('llama-server" --version', build_ollama)
        self.assertIn('payload_dir="dist/darwin-${ollama_arch}"', build_ollama)
        self.assertIn(
            'cp -a "${payload_dir}/." "${prefix}/ollama/"', build_ollama
        )
        self.assertIn('"${payload_dir}/lib/ollama/llama-server"', build_ollama)
        self.assertIn('"${payload_dir}/lib/ollama/llama-quantize"', build_ollama)

        # Ollama releases before the native-payload layout still use the old Go flow.
        self.assertIn("go generate ./...", build_ollama)
        self.assertIn('go build -ldflags=', build_ollama)

    def test_macos_ollama_030_verification_requires_native_helpers(self):
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")
        verification = self._extract_shell_function(
            script, "get_package_verification_info"
        )

        self.assertIn("ollama_minor >= 30", verification)
        self.assertIn(
            "/ollama/lib/ollama/llama-server", verification
        )
        self.assertIn(
            "/ollama/lib/ollama/llama-quantize", verification
        )
        self.assertIn("test -x", verification)
        self.assertIn("llama-server --version", verification)

    def test_macos_ollama_repins_env_flags_to_14_baseline(self):
        # dispatcher 为所有包导出的 CFLAGS/CXXFLAGS/LDFLAGS 以全局
        # BUILD_OS_MIN_VERSION(12.00) 为基线，x86_64 还带
        # `-target x86_64-apple-macos12.00`。CMake 首次 configure 会用环境变量
        # 初始化 CMAKE_<LANG>_FLAGS/链接器缓存；clang 中 -target 优先于
        # -mmacosx-version-min，Ollama 子构建（llama.cpp/MLX）的实际部署目标
        # 会被拉低到 12.0，x86_64 上 libc++ 浮点 to_chars(13.3+) 直接编译失败。
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")
        build_ollama = self._extract_shell_function(script, "build_ollama")

        self.assertIn(
            'export CFLAGS="-isysroot ${SDK_PATH} -Qunused-arguments'
            ' -mmacosx-version-min=${macos_min} ${BUILD_CPU_ARCH}"',
            build_ollama,
        )
        self.assertIn(
            'export CXXFLAGS="${CFLAGS} -Wno-enum-constexpr-conversion"',
            build_ollama,
        )
        self.assertIn(
            'export LDFLAGS="-isysroot ${SDK_PATH}'
            ' -mmacosx-version-min=${macos_min} ${BUILD_CPU_ARCH}"',
            build_ollama,
        )
        self.assertNotIn("${BUILD_MACOS_TARGET}", build_ollama)
        self.assertNotIn("${BUILD_OS_MIN_VERSION}", build_ollama)

    def test_macos_ollama_gate_verifies_native_payload_minos(self):
        # 出口校验此前只覆盖 Go 二进制；llama-server/llama-quantize 等 native
        # 产物的 deployment target 漂移无法被发现，必须一并校验。
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")
        build_ollama = self._extract_shell_function(script, "build_ollama")

        self.assertRegex(
            build_ollama,
            r'verify_macos_min_version "\$\{payload_dir\}/lib/ollama/llama-server"'
            r' "\$\{macos_min\}"',
        )
        self.assertRegex(
            build_ollama,
            r'verify_macos_min_version "\$\{payload_dir\}/lib/ollama/llama-quantize"'
            r' "\$\{macos_min\}"',
        )

    def test_macos_openjdk_verify_accepts_modern_and_legacy_banners(self):
        # fa00370 把 openjdk 校验改成「匹配 openjdk version "<major> 前缀后回显
        # conf 版本」，但 Zulu 7/8 的 banner 是 legacy 形式（openjdk version
        # "1.8.0_504"）：既匹配不到该前缀，回显的 8.0.504 也不含 core_verify_package
        # 转换出的期望值 1.8.0_504，7/8 校验必然失败。本测试执行
        # get_package_verification_info 中 openjdk case 的真实代码行，并按
        # core_verify_package 的契约（7/8 legacy 转换 + grep -qFi 子串匹配，
        # 见 build_package core_verify_package 的 openjdk 分支）断言校验结果。
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")

        def banner_java(banner: str) -> str:
            return f'echo \'openjdk version "{banner}"\' >&2'

        # 现代版本：27 只打印 feature version，17 打印完整版本。
        self.assertTrue(self._core_verify_passes(
            "openjdk", "27.0.0",
            self._run_verify_case(script, "openjdk", "27.0.0",
                                  "openjdk/27/27.0.0/bin/java", banner_java("27")),
        ))
        self.assertTrue(self._core_verify_passes(
            "openjdk", "17.0.20.1",
            self._run_verify_case(script, "openjdk", "17.0.20.1",
                                  "openjdk/17/17.0.20.1/bin/java", banner_java("17.0.20.1")),
        ))
        # Zulu 7/8 legacy banner：本回归的核心场景，缺陷实现下必然失败。
        self.assertTrue(self._core_verify_passes(
            "openjdk", "8.0.504",
            self._run_verify_case(script, "openjdk", "8.0.504",
                                  "openjdk/8/8.0.504/bin/java", banner_java("1.8.0_504")),
        ))
        self.assertTrue(self._core_verify_passes(
            "openjdk", "7.0.352",
            self._run_verify_case(script, "openjdk", "7.0.352",
                                  "openjdk/7/7.0.352/bin/java", banner_java("1.7.0_352")),
        ))
        # 反例 1：bin/java 缺失 → test -x 短路，空输出不得被误判为通过。
        self.assertFalse(self._core_verify_passes(
            "openjdk", "8.0.504",
            self._run_verify_case(script, "openjdk", "8.0.504",
                                  "openjdk/8/8.0.504/bin/java", "", install_bin=False),
        ))
        # 反例 2：java 损坏，报错文本包含含版本的安装路径（如
        # ".../openjdk/27/27.0.0/bin/java"），gate 必须拦住这种伪通过。
        self.assertFalse(self._core_verify_passes(
            "openjdk", "27.0.0",
            self._run_verify_case(script, "openjdk", "27.0.0",
                                  "openjdk/27/27.0.0/bin/java",
                                  'echo "exec format error: $0" >&2'),
        ))

    def test_macos_php_verify_keeps_banner_output_for_core_match(self):
        # get_package_verification_info 里 "php") 出现两次：前一个（raw `php -v`，
        # 满足 core_verify_package 的「输出内容」契约）实际生效；后一个用
        # `grep -q` 静默吞掉输出，若它变成可达分支，core_verify 的子串匹配
        # （grep -qFi "$versionToMatch"）作用在空输出上必然失败——与 openjdk 7/8
        # P1 同类。本测试钉住可达分支的真实行为（stable 与 dev——后者由
        # core_verify 截断日期后缀后匹配），防止分支重排或合并后静默回归。
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")

        self.assertTrue(self._core_verify_passes(
            "php", "8.4.27",
            self._run_verify_case(script, "php", "8.4.27",
                                  "php/8.4/8.4.27/bin/php",
                                  'echo "PHP 8.4.27 (cli) (built: Nov  1 2026)"'),
        ))
        self.assertTrue(self._core_verify_passes(
            "php", "8.5.0-dev-20251105",
            self._run_verify_case(script, "php", "8.5.0-dev-20251105",
                                  "php/8.5/8.5.0-dev-20251105/bin/php",
                                  'echo "PHP 8.5.0-dev (cli) (built: Nov  5 2025)"'),
        ))

    def test_macos_dotnetsdk_verify_cannot_be_faked_by_error_text(self):
        # dotnetsdk 现代分支（packageVersion == major.*，如 11.0.100-rc1）此前
        # 缺少 test -x 守卫且未丢弃 stderr：dotnet 缺失/损坏时，zsh 的
        # "no such file or directory: .../dotnetsdk/11.0/11.0.100-rc1/dotnet"
        # 报错文本被 core_verify_package 的 2>&1 捕获，其中含 conf 版本子串，
        # 校验伪通过（与 openjdk 反例同类）。本测试执行该 case 的真实代码行，
        # 按 core_verify_package 契约（grep -qFi 子串匹配）断言。
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")

        # 正例：SDK 报告的运行时版本（11.0.100-rc.1.x）与 conf 短名
        # （11.0.100-rc1）不同，grep 命中 major 后回显 conf 版本。
        self.assertTrue(self._core_verify_passes(
            "dotnetsdk", "11.0.100-rc1",
            self._run_verify_case(script, "dotnetsdk", "11.0.100-rc1",
                                  "dotnetsdk/11.0/11.0.100-rc1/dotnet",
                                  'echo "11.0.100-rc.1.26425.128"'),
        ))
        # 反例 1：dotnet 缺失 → test -x 短路，报错文本不得构成伪通过。
        self.assertFalse(self._core_verify_passes(
            "dotnetsdk", "11.0.100-rc1",
            self._run_verify_case(script, "dotnetsdk", "11.0.100-rc1",
                                  "dotnetsdk/11.0/11.0.100-rc1/dotnet",
                                  "", install_bin=False),
        ))
        # 反例 2：dotnet 损坏，stderr 报错含含版本的安装路径（如
        # ".../dotnetsdk/11.0/11.0.100-rc1/dotnet"），不得落入外层捕获。
        self.assertFalse(self._core_verify_passes(
            "dotnetsdk", "11.0.100-rc1",
            self._run_verify_case(script, "dotnetsdk", "11.0.100-rc1",
                                  "dotnetsdk/11.0/11.0.100-rc1/dotnet",
                                  'echo "exec format error: $0" >&2'),
        ))
        # legacy 行（2.1.202 → dotnetsdk/2.0 路径）：test -x 已有，但损坏
        # 二进制的 stderr 报错此前会逃逸到外层捕获构成伪通过，一并钉住。
        self.assertTrue(self._core_verify_passes(
            "dotnetsdk", "2.1.202",
            self._run_verify_case(script, "dotnetsdk", "2.1.202",
                                  "dotnetsdk/2.0/2.1.202/dotnet",
                                  'echo "2.1.202"'),
        ))
        self.assertFalse(self._core_verify_passes(
            "dotnetsdk", "2.1.202",
            self._run_verify_case(script, "dotnetsdk", "2.1.202",
                                  "dotnetsdk/2.0/2.1.202/dotnet",
                                  'echo "exec format error: $0" >&2'),
        ))

    def _extract_case_block(self, script: str, package: str) -> str:
        verification = self._extract_shell_function(
            script, "get_package_verification_info"
        )
        block_match = re.search(
            rf'"{package}"\)\n(?P<block>.*?)\n\s*;;', verification, re.DOTALL
        )
        self.assertIsNotNone(block_match, f"{package} case block not found")
        return block_match.group("block")

    def _run_verify_case(self, script: str, package: str, version: str,
                         bin_relpath: str, binary_script: str,
                         install_bin: bool = True) -> str:
        """在 zsh 中执行 get_package_verification_info 对应 case 的真实代码行，
        返回 eval verify_command 的合并输出（复刻 core_verify_package 的捕获方式）。"""
        with tempfile.TemporaryDirectory() as tmp:
            if install_bin:
                bin_rel = Path(bin_relpath)
                bin_dir = Path(tmp) / bin_rel.parent
                bin_dir.mkdir(parents=True)
                binary = bin_dir / bin_rel.name
                binary.write_text(f"#!/bin/sh\n{binary_script}\n", encoding="utf-8")
                binary.chmod(0o755)
            harness_parts = []
            # case block 可能调用脚本内的 helper（如 dotnetsdk 的
            # _get_dotnetsdk_major_version），注入真实定义（_extract_shell_function
            # 只返回函数体，需补回函数头），避免测试走偏。
            try:
                harness_parts.append(
                    "_get_dotnetsdk_major_version() {\n"
                    + self._extract_shell_function(
                        script, "_get_dotnetsdk_major_version"
                    )
                    + "}"
                )
            except AssertionError:
                pass
            harness_parts.append(
                "run_case() {\n"
                f'    local packageName="{package}"\n'
                f'    local packageVersion="{version}"\n'
                f'    local SERVBAY_PACKAGE_FULL_PATH="{tmp}"\n'
                f"{self._extract_case_block(script, package)}\n"
                '    local output=$(eval "$verify_command" 2>&1)\n'
                '    print -r -- "$output"\n'
                "}\n"
                "run_case\n"
            )
            harness = "\n".join(harness_parts)
            result = subprocess.run(
                ["/bin/zsh", "-c", harness],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, f"harness failed: {result.stderr}"
            )
            return result.stdout

    @staticmethod
    def _core_verify_passes(package: str, version: str, output: str) -> bool:
        # 镜像 core_verify_package 的期望值转换：php dev 截断日期后缀、
        # openjdk 7/8 转 legacy 1.<major>.0_<patch>，其余直接用 conf 版本；
        # 对 verify_command 输出做不区分大小写的子串匹配（grep -qFi 语义）。
        expected = version
        php_dev = re.match(r"^([0-9]+\.[0-9]+\.[0-9]+-dev)-[0-9]{8}$", version)
        if package == "php" and php_dev:
            expected = php_dev.group(1)
        else:
            legacy = re.match(r"^([78])\.0\.([0-9]+)$", version)
            if package == "openjdk" and legacy:
                expected = f"1.{legacy.group(1)}.0_{legacy.group(2)}"
        return expected.lower() in output.lower()

    @staticmethod
    def _extract_shell_function(script, name):
        match = re.search(rf"^{name}\(\) \{{\n(?P<body>.*?)(?=^\}}\n)", script, re.MULTILINE | re.DOTALL)
        if not match:
            raise AssertionError(f"Function not found: {name}")
        return match.group("body")


if __name__ == "__main__":
    unittest.main()
