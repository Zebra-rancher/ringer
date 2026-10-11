from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402

KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")


def toml_string(value: object) -> str:
    return json.dumps(str(value))


class WorkerEnvTests(unittest.TestCase):
    def test_worker_env_strips_api_keys_and_keeps_the_rest(self) -> None:
        env = ringer.worker_env({**{k: "sk-test" for k in KEYS}, "PATH": "/bin", "HOME": "/h"})
        for key in KEYS:
            self.assertNotIn(key, env)
        self.assertEqual("/bin", env["PATH"])
        self.assertEqual("/h", env["HOME"])

    def test_worker_process_never_sees_api_keys(self) -> None:
        # A real run: the "engine" is a shell that writes which key vars it can see.
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            (root / "home").mkdir()
            (root / "ringer-home").mkdir()
            config_path = root / "config.toml"
            manifest_path = root / "manifest.json"
            probe = "; ".join(f'echo "{k}=${{{k}:+present}}" >> env.txt' for k in KEYS)
            config_path.write_text(
                "\n".join(
                    [
                        f"state_dir = {toml_string(root / 'state')}",
                        "[eval]",
                        'backend = "jsonl"',
                        f"jsonl_path = {toml_string(root / 'runs.jsonl')}",
                        "[artifact]",
                        "enabled = false",
                        "[engines.envprobe]",
                        'bin = "/bin/sh"',
                        f'args_template = ["-c", {toml_string(probe)}, "{{spec}}"]',
                        "sandbox_args = []",
                        "full_access_args = []",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_name": "worker-env-test",
                        "workdir": str(root / "work"),
                        "worktrees": False,
                        "tasks": [
                            {
                                "key": "env-probe",
                                "engine": "envprobe",
                                "spec": (
                                    "Record which API key environment variables are visible to "
                                    "this worker process, one line per variable, in env.txt."
                                ),
                                "check": "test -s env.txt || { echo FAIL: env.txt missing; exit 1; }",
                                "expect_files": ["env.txt"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, **{k: "sk-should-not-leak" for k in KEYS}}
            env.update(HOME=str(root / "home"), RINGER_HOME=str(root / "ringer-home"),
                       XDG_CONFIG_HOME=str(root / "xdg-config"))
            proc = subprocess.run(
                [sys.executable, "ringer.py", "run", str(manifest_path), "--config",
                 str(config_path), "--no-dashboard", "--identity", "env-test"],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=60,
            )
            written = list((root / "work").rglob("env.txt"))
            self.assertTrue(written, proc.stdout + proc.stderr)
            lines = written[0].read_text(encoding="utf-8").split()
            self.assertEqual([f"{k}=" for k in KEYS], lines)


if __name__ == "__main__":
    unittest.main()
