"""The llama.cpp RPC build helper: discovery + argv logic, no compiler needed.

`scripts/build_llama_rpc.py` exists so the layer-split build stops depending on
someone's shell history. Everything that decides *what* it runs is pure and
tested here; the compile itself is only exercised by actually running it.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import build_llama_rpc as b  # noqa: E402

SUBMODULE = "platforms/android/app/src/main/cpp/llama.cpp"
STATUS_INIT = f" 6703d7894c70e8b076ce4608157d056e42e6889c {SUBMODULE} (v1)\n"
STATUS_EMPTY = f"-6703d7894c70e8b076ce4608157d056e42e6889c {SUBMODULE}\n"


class SubmoduleStatusTestCase(unittest.TestCase):
    def test_initialised_submodule_reports_its_commit(self):
        self.assertEqual(b.parse_submodule_status(STATUS_INIT, SUBMODULE),
                         "6703d7894c70e8b076ce4608157d056e42e6889c")

    def test_uninitialised_submodule_is_an_error_not_a_silent_build(self):
        with self.assertRaises(RuntimeError) as ctx:
            b.parse_submodule_status(STATUS_EMPTY, SUBMODULE)
        self.assertIn("git submodule update --init", str(ctx.exception))

    def test_other_submodules_are_ignored(self):
        text = STATUS_INIT + f" 6c977e2 nrr (6c977e2)\n"
        self.assertEqual(b.parse_submodule_status(text, SUBMODULE),
                         "6703d7894c70e8b076ce4608157d056e42e6889c")
        self.assertEqual(b.parse_submodule_status(text, "nrr"), "6c977e2")

    def test_absent_path_returns_empty(self):
        self.assertEqual(b.parse_submodule_status(STATUS_INIT, "nope"), "")
        self.assertEqual(b.parse_submodule_status("", SUBMODULE), "")

    def test_a_checkout_at_a_different_commit_is_still_read(self):
        """`git submodule status` marks a checkout that differs from the pin."""
        line = f"+95887577ab5fead779581a7030a83c7752ff3234 {SUBMODULE} (v0.5.0-59)\n"
        self.assertEqual(b.parse_submodule_status(line, SUBMODULE),
                         "95887577ab5fead779581a7030a83c7752ff3234")

    def test_the_recorded_pin_is_parsed_from_ls_tree(self):
        text = (f"160000 commit 6703d7894c70e8b076ce4608157d056e42e6889c"
                f"\t{SUBMODULE}\n")
        self.assertEqual(b.parse_ls_tree_commit(text),
                         "6703d7894c70e8b076ce4608157d056e42e6889c")
        self.assertEqual(b.parse_ls_tree_commit(""), "")
        self.assertEqual(b.parse_ls_tree_commit("nonsense"), "")


class PopulatedTestCase(unittest.TestCase):
    def test_empty_directory_is_not_populated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(b.state_is_populated(Path(tmp)))
            Path(tmp, "CMakeLists.txt").write_text("x", encoding="utf-8")
            self.assertTrue(b.state_is_populated(Path(tmp)))

    def test_missing_directory_is_not_populated(self):
        self.assertFalse(b.state_is_populated(Path("G:/definitely/not/here")))


class ToolDiscoveryTestCase(unittest.TestCase):
    def test_explicit_path_wins(self):
        found = b.find_tool(("cmake",), explicit="C:/tools/cmake.exe",
                            which=lambda _n: "C:/path/cmake", exists=lambda _p: True)
        self.assertEqual(found, "C:/tools/cmake.exe")

    def test_explicit_path_must_exist(self):
        found = b.find_tool(("cmake",), explicit="C:/nope/cmake.exe",
                            which=lambda _n: "C:/path/cmake", exists=lambda _p: False)
        self.assertEqual(found, "C:/path/cmake")

    def test_android_sdk_dirs_are_preferred_over_path(self):
        """The SDK's CMake/Ninja are ordinary host tools and are already here."""
        sdk = ["G:/Android/Sdk/cmake/3.22.1/bin"]
        found = b.find_tool(("cmake.exe",), extra_dirs=sdk,
                            which=lambda _n: "C:/somewhere/cmake.exe",
                            exists=lambda p: "3.22.1" in str(p))
        self.assertIn("3.22.1", str(found))
        self.assertNotIn("somewhere", str(found))

    def test_missing_everywhere_is_none(self):
        self.assertIsNone(b.find_tool(("cmake",), which=lambda _n: None,
                                      exists=lambda _p: False))

    def test_sdk_cmake_dirs_come_from_the_environment(self):
        env = {"ANDROID_HOME": "G:/Android/Sdk"}
        hits = b.android_cmake_dirs(env=env, globber=lambda pattern: [
            "G:/Android/Sdk/cmake/3.22.1/bin"])
        self.assertEqual(hits, ["G:/Android/Sdk/cmake/3.22.1/bin"])


class MsvcTestCase(unittest.TestCase):
    def test_non_windows_needs_no_environment(self):
        with mock.patch.object(b.platform, "system", return_value="Linux"):
            self.assertEqual(b.find_vcvars(), "")

    def test_developer_prompt_already_has_the_compiler(self):
        with mock.patch.object(b.platform, "system", return_value="Windows"):
            self.assertEqual(b.find_vcvars(which=lambda _n: "cl.exe"), "")

    def test_vswhere_locates_vcvars(self):
        install = "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community"
        with mock.patch.object(b.platform, "system", return_value="Windows"):
            found = b.find_vcvars(
                which=lambda _n: None,
                exists=lambda _p: True,
                run=lambda _cmd: install + "\n")
        self.assertEqual(found, os.path.join(
            install, "VC", "Auxiliary", "Build", "vcvars64.bat"))

    def test_missing_vswhere_is_not_an_error(self):
        with mock.patch.object(b.platform, "system", return_value="Windows"):
            self.assertEqual(b.find_vcvars(which=lambda _n: None,
                                           exists=lambda _p: False), "")

    def test_msvc_environment_goes_through_a_temp_script(self):
        """A quoted path inside an inline ``cmd /c`` string does not survive
        Windows argv quoting -- exactly how the first real build failed."""
        script = b.msvc_env_script("C:\\Program Files\\VS\\vcvars64.bat")
        self.assertIn('call "C:\\Program Files\\VS\\vcvars64.bat" >nul', script)
        self.assertIn("set", script.splitlines()[-1])

    def test_environment_is_written_as_json_because_set_wraps(self):
        """`set` splits long values: PATH came back empty and CMake found no
        compiler. Python writes the environment to a file instead."""
        script = b.msvc_env_script("C:/vs/vcvars64.bat", python_exe="C:/py.exe",
                                   dump_path="C:/tmp/env")
        self.assertIn('set > "C:/tmp/env.set"', script)
        self.assertIn('"C:/py.exe" -c', script)
        self.assertIn("json.dumps(dict(os.environ))", script)
        self.assertIn('"C:/tmp/env.json"', script)

    def test_wrapped_set_output_is_rejoined(self):
        """Continuation lines belong to the previous variable, not to a new one."""
        env = b.parse_env_dump(
            "PATH=C:\\bin;C:\\other\r\n"
            "C:\\Program Files\\MSVC\\bin\r\n"
            "INCLUDE=C:\\inc\r\n"
            "=C:=C:\\cwd\r\n")
        self.assertIn("C:\\Program Files\\MSVC\\bin", env["PATH"])
        self.assertEqual(env["INCLUDE"], "C:\\inc")
        self.assertNotIn("C:\\Program Files\\MSVC\\bin", env)

    def test_environment_dump_is_parsed(self):
        env = b.parse_env_dump("PATH=C:/bin;C:/other\nINCLUDE=C:/inc\n"
                               "=::=::\\\nEMPTY=\n")
        self.assertEqual(env["INCLUDE"], "C:/inc")
        self.assertEqual(env["EMPTY"], "")
        self.assertNotIn("", env)
        self.assertNotIn("=::", env)

    def test_captured_environment_is_merged_not_replaced(self):
        captured = b.capture_msvc_env(
            "C:/vs/vcvars64.bat",
            run_capture=lambda _cmd: "",
            read_text=lambda _path, _limit=0: (
                "PATH=C:/msvc/bin\nVSCMD_ARG_TGT_ARCH=x64\n"))
        self.assertEqual(captured["VSCMD_ARG_TGT_ARCH"], "x64")
        base = {"BASE": "0"}
        merged = b.merged_env(captured, base=base)
        self.assertEqual(merged["BASE"], "0")          # base survives
        self.assertEqual(merged["VSCMD_ARG_TGT_ARCH"], "x64")
        self.assertNotIn("VSCMD_ARG_TGT_ARCH", base)  # base untouched

    def test_a_corrupt_json_dump_falls_back_to_the_set_dump(self):
        captured = b.capture_msvc_env(
            "C:/vs/vcvars64.bat",
            run_capture=lambda _cmd: "",
            read_text=lambda path, _limit=0: (
                "{not json" if str(path).endswith(".json")
                else "INCLUDE=C:/inc\n"))
        self.assertEqual(captured["INCLUDE"], "C:/inc")

    def test_json_dump_wins_over_the_set_fallback(self):
        captured = b.capture_msvc_env(
            "C:/vs/vcvars64.bat",
            run_capture=lambda _cmd: "",
            read_text=lambda path, _limit=0: (
                '{"PATH": "C:/msvc/bin", "INCLUDE": "C:/inc"}'
                if str(path).endswith(".json") else "PATH=C:/from-set"))
        self.assertEqual(captured["PATH"], "C:/msvc/bin")

    def test_no_vcvars_means_no_environment_capture(self):
        self.assertEqual(b.capture_msvc_env("", run_capture=lambda _c: "x=y"), {})


class ConfigureArgsTestCase(unittest.TestCase):
    def test_host_build_enables_rpc_and_the_server(self):
        args = b.host_configure_args("SRC", "BUILD")
        self.assertIn("-DGGML_RPC=ON", args)
        self.assertIn("-DLLAMA_BUILD_SERVER=ON", args)
        self.assertIn("-DLLAMA_BUILD_TOOLS=ON", args)
        self.assertIn("-DLLAMA_CURL=OFF", args)     # no network dep in a local build
        self.assertIn("-DCMAKE_BUILD_TYPE=Release", args)
        self.assertIn("Ninja", args)
        self.assertIn("SRC", args)

    def test_android_build_is_the_peripheral_only(self):
        args = b.android_configure_args("SRC", "BUILD", "G:/Android/Sdk/ndk/27.0")
        joined = " ".join(args)
        self.assertIn("android.toolchain.cmake", joined)
        self.assertIn("-DANDROID_ABI=arm64-v8a", args)
        self.assertIn("-DANDROID_PLATFORM=android-28", args)
        self.assertIn("-DANDROID_STL=c++_static", args)
        self.assertIn("-DLLAMA_BUILD_SERVER=OFF", args)
        self.assertIn("G:/Android/Sdk/ndk/27.0", joined)

    def test_build_command_names_every_target(self):
        argv = b.build_command("cmake", "BUILD", b.HOST_TARGETS, jobs=8)
        self.assertEqual(argv[:4], ["cmake", "--build", "BUILD", "--target"])
        self.assertIn("llama-server", argv)
        self.assertIn("ggml-rpc-server", argv)
        self.assertIn("-j8", argv)

    def test_binaries_are_named_per_platform(self):
        win = b.expected_binaries("B", ("llama-server",), system="Windows")
        self.assertTrue(win["llama-server"].endswith("llama-server.exe"))
        nix = b.expected_binaries("B", ("llama-server",), system="Linux")
        self.assertTrue(nix["llama-server"].endswith("llama-server"))


class SourceFactsTestCase(unittest.TestCase):
    """The questions the KV-cache phase depends on, answered from source."""

    def _source(self, files):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return root

    def test_kv_state_and_multi_device_rpc_are_detected(self):
        root = self._source({
            "include/llama.h": "llama_state_seq_save_file(a, b, c);\n",
            "common/arg.cpp": '{"--rpc", "-r", ...} and `--slot-save-path`\n',
            "tools/rpc/rpc-server.cpp": "ggml_backend_rpc_init(...)\n",
        })
        report = b.verify_sources(root)
        self.assertTrue(report["kv_state_save"])
        self.assertTrue(report["slot_save_path"])
        self.assertTrue(report["rpc"])
        self.assertGreater(report["scanned"], 0)

    def test_an_older_tree_reports_no_rather_than_assuming(self):
        root = self._source({"include/llama.h": "int nothing_here();\n"})
        report = b.verify_sources(root)
        self.assertEqual(report["kv_state_save"], [])
        self.assertEqual(report["slot_save_path"], [])
        self.assertEqual(report["rpc"], [])

    def test_unrelated_trees_are_not_scanned(self):
        root = self._source({
            "examples/big.cpp": "--rpc llama_state_seq_save_file slot-save-path\n",
        })
        report = b.verify_sources(root)
        self.assertEqual(report["rpc"], [])


class DeviceVerificationTestCase(unittest.TestCase):
    """A build that compiles but loads no backend is not a working build.

    The first Windows build produced a `llama-server.exe` that reported
    "failed to find ggml_backend_init" for its own DLLs and listed no devices:
    everything compiled, and nothing could offload.
    """

    def test_devices_are_parsed(self):
        text = ("0.00 E load_backend: failed to find ggml_backend_init\n"
                "Available devices:\n"
                "  RPC0: 192.168.1.164:50052 (5425 MiB, 5425 MiB free)\n"
                "  CPU: AMD Ryzen 7 3700X\n")
        devices = b.parse_available_devices(text)
        self.assertEqual(len(devices), 2)
        self.assertIn("RPC0", devices[0])

    def test_none_is_an_empty_list_not_a_device(self):
        self.assertEqual(
            b.parse_available_devices("Available devices:\n  (none)\n"), [])
        self.assertEqual(b.parse_available_devices("something else"), [])

    def test_verify_devices_reports_the_problem(self):
        devices, raw = b.verify_devices(
            "llama-server",
            run_capture=lambda _cmd: "Available devices:\n  (none)\n")
        self.assertEqual(devices, [])
        self.assertIn("(none)", raw)

    def test_host_build_is_static_so_backends_actually_load(self):
        args = b.host_configure_args("SRC", "BUILD")
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", args)
        self.assertIn("-DGGML_BACKEND_DL=OFF", args)

    def test_the_gate_is_the_rpc_flag_not_the_device_list(self):
        """A static build answers "(none)" to --list-devices and still offloads."""
        self.assertTrue(b.verify_rpc_flag(
            "llama-server",
            run_capture=lambda _cmd: "usage: llama-server ...\n  --rpc SERVERS\n"))
        self.assertFalse(b.verify_rpc_flag(
            "llama-server",
            run_capture=lambda _cmd: "usage: llama-server ...\n  --port N\n"))
        self.assertFalse(b.verify_rpc_flag(
            "llama-server", run_capture=lambda _cmd: ""))

    def test_android_build_keeps_the_documented_shared_backends(self):
        args = b.android_configure_args("SRC", "BUILD", "NDK")
        self.assertIn("-DGGML_BACKEND_DL=ON", args)
        self.assertIn("-DBUILD_SHARED_LIBS=ON", args)


class DryRunTestCase(unittest.TestCase):
    """`--dry-run` proves the orchestration without a compiler on the box."""

    def test_dry_run_plans_the_host_build(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = b.main(["--host", "--dry-run"])
        self.assertEqual(rc, 0)
        text = buffer.getvalue()
        self.assertIn("GGML_RPC=ON", text)
        self.assertIn("llama-server", text)
        self.assertIn("ggml-rpc-server", text)
        self.assertIn("not written: dry-run", text)

    def test_android_without_ndk_is_refused(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            rc = b.main(["--android", "--dry-run", "--ndk", ""])
        self.assertEqual(rc, 2)
        self.assertIn("--ndk", buffer.getvalue())

    def test_uninitialised_submodule_is_refused_with_the_fix(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with mock.patch.object(b, "state_is_populated", return_value=False):
            with contextlib.redirect_stderr(buffer):
                rc = b.main(["--host"])
        self.assertEqual(rc, 2)
        self.assertIn("git submodule update --init", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

