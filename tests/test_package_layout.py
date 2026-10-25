import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "tested_package_layout", ROOT / "runtime/ci/package_layout.py"
        )
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def layout(self, name, version, os="windows", arch="x64"):
        return self.helper.package_layout(name, version, os=os, arch=arch)

    def test_windows_php_returns_archive_relative_layout_and_native_entrypoint(self):
        self.assertEqual(self.layout("php", "8.3.36"), {
            "name": "php", "os": "windows", "arch": "x86_64",
            "package_root": "php/8.3", "config_root": "php/8.3",
            "entrypoint": {"path": "php/8.3/bin/php.exe", "type": "binary"},
        })

    def test_windows_service_entrypoints_use_v2_directories(self):
        for name, version, root, executable in [
            ("redis", "7.4.11", "redis/7.4", "bin/redis-server.exe"),
            ("postgresql", "18.6", "postgresql/18", "bin/postgres.exe"),
            ("nginx", "1.31.8", "nginx", "sbin/nginx.exe"),
            ("caddy", "2.11.7", "caddy", "bin/caddy.exe"),
        ]:
            with self.subTest(name=name):
                result = self.layout(name, version)
                self.assertEqual(result["package_root"], root)
                self.assertEqual(result["config_root"], root)
                self.assertEqual(result["entrypoint"], {
                    "path": root + "/" + executable, "type": "binary",
                })

    def test_additional_windows_packages_use_producer_verified_roots_and_entries(self):
        cases = [
            ("go", "1.27.3", "go/1.27", "bin/go.exe", "binary"),
            ("ruby", "3.4.12", "ruby/3.4", "bin/ruby.exe", "binary"),
            ("openjdk", "21.0.13", "openjdk/21", "bin/java.exe", "binary"),
            ("maven", "3.9.15", "maven", "bin/mvn.cmd", "binary"),
            ("mailpit", "1.32.0", "mailpit", "bin/mailpit.exe", "binary"),
            ("ollama", "0.40.10", "ollama", "bin/ollama.exe", "binary"),
            ("deno", "2.9.8", "deno", "bin/deno.exe", "binary"),
            ("bun", "1.4.4", "bun", "bin/bun.exe", "binary"),
            ("ngrok", "3.39.13", "ngrok", "bin/ngrok.exe", "binary"),
            ("git", "2.56.0", "git", "cmd/git.exe", "binary"),
            ("openssl", "3.6.1", "openssl", "bin/openssl.exe", "binary"),
            ("ai-gateway", "1.0.0", "ai-gateway", "bin/servbay-ai-gateway.exe", "binary"),
            ("disk-cleaner", "1.0.0", "disk-cleaner", "bin/servbay-diskcleaner.exe", "binary"),
        ]
        for name, version, root, entry, kind in cases:
            with self.subTest(name=name):
                result = self.layout(name, version)
                self.assertEqual(result["package_root"], root)
                self.assertEqual(result["config_root"], root)
                self.assertEqual(result["entrypoint"], {
                    "path": root + "/" + entry, "type": kind,
                })
        for target_os in ("windows", "macos"):
            for name, version, root in (("adminer", "6.2.0", "adminer"),
                                        ("phpmyadmin", "5.2.3", "phpmyadmin")):
                with self.subTest(os=target_os, name=name):
                    self.assertEqual(self.layout(name, version, os=target_os)["entrypoint"], {
                        "path": root + "/index.php", "type": "php-script",
                        "interpreter": "php",
                    })

    def test_versioned_packages_keep_their_verified_series(self):
        for name, version, root in [
            ("php", "7.4.33.1", "php/7.4"),
            ("go", "1.27.3", "go/1.27"),
            ("mysql", "8.4.13", "mysql/8.4"),
            ("mariadb", "11.4.13", "mariadb/11.4"),
            ("mongodb", "8.2.12", "mongodb/8.2"),
            ("ruby", "3.4.11", "ruby/3.4"),
            ("node", "24.22.1", "node/24"),
            ("openjdk", "21.0.12.1", "openjdk/21"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.layout(name, version)["package_root"], root)

    def test_macos_series_preserve_dotnet_special_case(self):
        for name, version, root, executable in [
            ("dotnetsdk", "2.1.202", "dotnetsdk/2.0", "dotnet"),
            ("dotnetsdk", "8.0.408", "dotnetsdk/8.0", "dotnet"),
            ("mono", "6.12.0", "mono/6.12", "bin/mono"),
            ("python", "3.13.17", "python/3.13", "bin/python3.13"),
        ]:
            with self.subTest(name=name, version=version):
                result = self.layout(name, version, os="macos", arch="arm64")
                self.assertEqual(result["package_root"], root)
                self.assertEqual(result["config_root"], root)
                self.assertEqual(result["entrypoint"]["path"], root + "/" + executable)

    def test_windows_python_preserves_the_native_distribution_subtree(self):
        result = self.layout("python", "3.13.17")
        self.assertEqual(result["package_root"], "python/3.13")
        self.assertEqual(result["config_root"], "python/3.13")
        self.assertEqual(result["entrypoint"], {
            "path": "python/3.13/python.exe", "type": "binary",
        })
        macos = self.layout("python", "3.13.17", os="macos")
        self.assertEqual(result["package_root"], macos["package_root"])
        self.assertEqual(result["config_root"], macos["config_root"])

    def test_composer_mainline_and_lts_are_php_scripts_on_both_platforms(self):
        for os in ("windows", "macos"):
            for version, series in (("2.10.3", "2"), ("2.2.30", "2.2")):
                with self.subTest(os=os, version=version):
                    result = self.layout("composer", version, os=os)
                    root = "composer/" + series
                    self.assertEqual(result["package_root"], root)
                    self.assertEqual(result["config_root"], root)
                    self.assertEqual(result["entrypoint"], {
                        "path": root + "/bin/composer", "type": "php-script",
                        "interpreter": "php",
                    })

    def test_rust_and_tunnels_are_flat_without_tunnel_parent(self):
        for name, version, executable in [
            ("rust", "1.99.1", "rustc"),
            ("cloudflared", "2026.10.3", "cloudflared"),
            ("frp", "0.71.1", "frpc"),
            ("ngrok", "3.39.12", "ngrok"),
            ("pinggy", "0.6.1", "pinggy"),
        ]:
            for os, suffix in (("windows", ".exe"), ("macos", "")):
                with self.subTest(name=name, os=os):
                    result = self.layout(name, version, os=os)
                    self.assertEqual(result["package_root"], name)
                    self.assertEqual(result["config_root"], name)
                    self.assertEqual(result["entrypoint"]["path"],
                                     name + "/bin/" + executable + suffix)
        self.assertEqual(self.layout("simple-acme", "2.4.1")["entrypoint"], {
            "path": "simple-acme/wacs.exe", "type": "binary",
        })
        with self.assertRaises(ValueError):
            self.layout("simple-acme", "2.4.1", os="macos")

    def test_platforms_share_package_and_configuration_roots(self):
        for name, version in [("php", "8.3.36"), ("redis", "7.4.11"),
                              ("postgresql", "18.6"), ("nginx", "1.31.8"),
                              ("node", "24.22.1"), ("apache", "2.4.69")]:
            with self.subTest(name=name):
                windows = self.layout(name, version)
                macos = self.layout(name, version, os="macos", arch="arm64")
                for key in ("package_root", "config_root"):
                    self.assertEqual(windows[key], macos[key])
                self.assertEqual(macos["arch"], "arm64")
                self.assertEqual(macos["os"], "macos")
        self.assertEqual(self.layout("nginx", "1.31.8", os="macos")["entrypoint"],
                         {"path": "nginx/sbin/nginx", "type": "binary"})

    def test_recipe_aliases_resolve_to_public_package_names(self):
        for alias, name, version in [("java", "openjdk", "21.0.12.1"),
                                     ("httpd", "apache", "2.4.69")]:
            with self.subTest(alias=alias):
                self.assertEqual(self.layout(alias, version), self.layout(name, version))

    def test_architecture_alias_is_limited_to_existing_x64_spelling(self):
        for value, expected in [("x64", "x86_64"), ("x86_64", "x86_64"),
                                ("arm64", "arm64")]:
            self.assertEqual(self.helper.normalize_arch(value), expected)
        for value in ("amd64", "X64", "x86", "i686", "aarch64", "", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.helper.normalize_arch(value)

    def test_windows_rejects_unimplemented_architectures(self):
        for arch in ("arm64", "x86", "amd64"):
            with self.subTest(arch=arch), self.assertRaises(ValueError):
                self.layout("php", "8.3.36", arch=arch)

    def test_os_labels_are_normalized_without_guessing_platforms(self):
        self.assertEqual(self.layout("php", "8.3.36", os="Windows")["os"], "windows")
        self.assertEqual(self.layout("php", "8.3.36", os="macOS")["os"], "macos")
        for os in ("linux", "win32", "darwin", "", None, []):
            with self.subTest(os=os), self.assertRaises(ValueError):
                self.layout("php", "8.3.36", os=os)

    def test_unknown_and_unverified_packages_fail_instead_of_guessing_layout(self):
        for name in ("unknown", "runtime", "devlib",
                     "dotnetsdk", "mono", "", None, []):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.layout(name, "3.13.17")
        with self.assertRaisesRegex(ValueError, "Windows producer"):
            self.layout("typesense", "30.2")
        self.assertEqual(self.layout("typesense", "30.2", os="macos", arch="arm64")["entrypoint"], {
            "path": "typesense/bin/typesense-server", "type": "binary",
        })

    def test_unsafe_versions_fail_even_for_flat_packages(self):
        for version in ("", "..", "../8.3", "8.3/evil", "8.3\\evil", "C:8.3",
                        "8.3:stream", "8.3.", "8..3", "8.3 ", " 8.3", "8.3\n",
                        "8.3\x00", "8.3;id", "8.3$(id)", "latest", None, 8.3):
            for name in ("php", "nginx"):
                with self.subTest(name=name, version=version), self.assertRaises(ValueError):
                    self.layout(name, version)

    def test_real_prerelease_and_backport_versions_remain_accepted(self):
        for name, version, root in [("php", "8.6.0beta3", "php/8.6"),
                                    ("php", "7.4.33-1", "php/7.4"),
                                    ("go", "1.21rc3", "go/1.21"),
                                    ("postgresql", "19.beta2", "postgresql/19")]:
            with self.subTest(name=name, version=version):
                self.assertEqual(self.layout(name, version)["package_root"], root)

    def test_unmapped_series_fail_explicitly(self):
        for name, version in [("php", "8"), ("redis", "7"),
                              ("postgresql", "9.6.24"), ("composer", "1.10.27"),
                              ("composer", "3.0.0")]:
            with self.subTest(name=name, version=version), self.assertRaises(ValueError):
                self.layout(name, version)


if __name__ == "__main__":
    unittest.main()
