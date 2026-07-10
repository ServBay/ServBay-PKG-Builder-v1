import re
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
        self.assertIn('-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0', build_ollama)
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
        self.assertIn('lipo -verify_arch "$cmake_arch" "$payload"', build_ollama)
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

    @staticmethod
    def _extract_shell_function(script, name):
        match = re.search(rf"^{name}\(\) \{{\n(?P<body>.*?)(?=^\}}\n)", script, re.MULTILINE | re.DOTALL)
        if not match:
            raise AssertionError(f"Function not found: {name}")
        return match.group("body")


if __name__ == "__main__":
    unittest.main()
