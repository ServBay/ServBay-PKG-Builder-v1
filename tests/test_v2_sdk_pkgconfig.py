import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SDKPkgconfigTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime-v2/ci/sdk_pkgconfig.py"
        self.assertTrue(path.is_file(), "Explicit SDK pkg-config metadata is missing")
        spec = importlib.util.spec_from_file_location("sdk_pkgconfig", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_actual_sdk_ffi_descriptor_compiles_and_runs_without_homebrew_libraries(self):
        sdk = Path(subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], check=True,
                                  capture_output=True, text=True).stdout.strip())
        pc = self.root / "build-only-pc"
        self.module.create_sdk_pkgconfig(sdk, pc)
        env = dict(os.environ, PKG_CONFIG_PATH=str(pc), PKG_CONFIG_LIBDIR=str(pc))
        flags = subprocess.run(["/usr/local/bin/pkg-config", "--cflags", "--libs", "libffi"],
                               check=True, env=env, capture_output=True, text=True).stdout
        source = self.root / "probe.c"
        source.write_text('#include <ffi.h>\n#include <stdio.h>\nint answer(void) { return 42; }\n'
                          'int main(void) { ffi_cif cif; ffi_arg result = 0; '
                          'if (ffi_prep_cif(&cif, FFI_DEFAULT_ABI, 0, &ffi_type_sint, 0) != FFI_OK) return 1; '
                          'ffi_call(&cif, FFI_FN(answer), &result, 0); printf("%lu\\n", (unsigned long)result); '
                          'return result != 42; }\n')
        binary = self.root / "probe"
        built = subprocess.run(["/usr/bin/clang", "-isysroot", str(sdk), str(source), "-o", str(binary), *shlex.split(flags)],
                               capture_output=True, text=True)
        self.assertEqual(0, built.returncode, built.stderr)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        self.assertEqual("42", result.stdout.strip())
        links = subprocess.run(["/usr/bin/otool", "-L", str(binary)], check=True, capture_output=True, text=True).stdout
        self.assertIn("/usr/lib/libffi.dylib", links)
        self.assertNotIn("/usr/local/", links)
        self.assertNotIn("/opt/homebrew/", links)

    def test_missing_sdk_header_or_stub_fails_without_fabricating_capability(self):
        sdk = self.root / "SDK"
        sdk.mkdir()
        with self.assertRaises(ValueError):
            self.module.create_sdk_pkgconfig(sdk, self.root / "pc")
        self.assertFalse((self.root / "pc/libffi.pc").exists())

    def test_unknown_header_version_is_rejected(self):
        sdk = self.root / "SDK"
        (sdk / "usr/include/ffi").mkdir(parents=True)
        (sdk / "usr/lib").mkdir()
        (sdk / "usr/include/ffi/ffi.h").write_text("/* FFI header without a known version */\n")
        (sdk / "usr/lib/libffi.tbd").write_text("stub")
        with self.assertRaises(ValueError):
            self.module.create_sdk_pkgconfig(sdk, self.root / "pc")


if __name__ == "__main__":
    unittest.main()
