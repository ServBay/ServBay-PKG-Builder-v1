"""minos 门禁不得把发行树 testdata/ 里的 Mach-O 测试夹具当运行时二进制。

go 1.27.3 双架构首跑（run 39997404749）实测：go 发行 tarball 的
src/debug/dwarf/testdata/typedef.macho 无 LC_BUILD_VERSION，校验器扫到即
raise "Mach-O minimum OS is missing"。夹具不是运行时二进制，testdata 段
路径必须跳过（用户已授权该扫描范围修正）。
"""

import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("tested_package_meta", ROOT / "runtime/ci/package_meta.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GoMinosTestdataSkipTests(unittest.TestCase):
    def test_testdata_macho_fixtures_are_exempt_from_the_minos_gate(self):
        module = load_module()
        # 一个带 minos 的真实 Mach-O 夹在 bin/go；testdata/ 下的成员首四字节
        # 是 Mach-O magic 但内容为占位（没有可解析的 load commands）。
        real = bytearray(b"\xcf\xfa\xed\xfe" + b"\x00" * 4096)
        fixture = bytearray(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        archive_path = Path(tempfile.mkdtemp(prefix="minos-guard-")) / "go.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, blob in (("go/1.27/bin/go", bytes(real)),
                               ("go/1.27/src/debug/dwarf/testdata/typedef.macho", bytes(fixture))):
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                archive.addfile(info, io.BytesIO(blob))

        calls = {"otool": [], "file": []}

        def fake_run(command, **kwargs):
            if command[:2] == ["/usr/bin/file", "-b"]:
                calls["file"].append(command)
                return mock.Mock(returncode=0, stdout="Mach-O 64-bit executable", stderr="")
            if command[:2] == ["/usr/bin/otool", "-l"]:
                target = str(command[2])
                calls["otool"].append(target)
                # testdata 夹具没有任何 load commands 可解析（与 go 夹具一致）
                if "testdata" in target:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0,
                                 stdout="   cmd LC_BUILD_VERSION\ncmdsize 32\n vers 12.0\n"
                                        "    minos 12.0\n",
                                 stderr="")
            raise AssertionError("unexpected subprocess call: %r" % (command[:2],))

        with mock.patch.object(module.subprocess, "run", fake_run):
            # 旧行为：testdata 夹具无 minos → ValueError；修复后必须跳过并放行。
            minimum = module.minimum_os_from_archive(archive_path)

        self.assertEqual("12.0", minimum)
        # 跳过发生在 file 分类之前：testdata 夹具根本不应进入分类与门禁。
        self.assertEqual(1, len(calls["file"]))
        self.assertEqual(1, len(calls["otool"]),
                         "the testdata fixture must never reach the minos gate")


if __name__ == "__main__":
    unittest.main()
