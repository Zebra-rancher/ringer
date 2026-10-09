from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import ringer

ROOT = Path(__file__).resolve().parents[1]


class JevShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / 'calls.jsonl'
        self.client = self.root / 'jev_call.py'
        self.client.write_text(
            'import json\nfrom pathlib import Path\n'
            'def ask(source_class, state, questions, caller="unknown"):\n'
            f'    with Path({str(self.record)!r}).open("a") as f:\n'
            '        f.write(json.dumps([source_class, state, questions, caller]) + "\\n")\n'
            '    return {"lane": {"choice": "codex-sol-medium", "confidence": 0.91}}\n'
        )
        self.lanes_path = self.root / 'lanes.toml'
        self.lanes_path.write_text((ROOT / 'registry/lanes.toml').read_text())
        self.lanes = ringer.load_lanes(self.lanes_path)
        self.config = ringer.JevConfig(client=self.client, lanes=self.lanes_path)
        self.task = ringer.TaskSpec('one', 'x' * 13000, 'true')
        self.env = patch.dict(os.environ, {'RINGER_NO_JEV': '', 'RINGER_NO_SELF_UPDATE': '1',
                                         'RINGER_NO_CATALOG_REFRESH': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_lanes_and_bad_files(self):
        lane = self.lanes['codex-sol-medium']
        self.assertEqual(('codex', 'gpt-5.6-sol'), (lane.engine, lane.model))
        self.assertEqual(('-c', 'model_reasoning_effort=medium'), lane.engine_args)
        self.assertIn('Standard engineering', lane.criteria)
        self.assertEqual({}, ringer.load_lanes(self.root / 'missing'))
        self.lanes_path.write_text('[broken')
        self.assertEqual({}, ringer.load_lanes(self.lanes_path))
        self.lanes_path.write_text('[lanes.bad]\nengine=4')
        self.assertEqual({}, ringer.load_lanes(self.lanes_path))

    def test_state_question_and_check(self):
        state = ringer.jev_lane_state(self.task, 'c' * 5000)
        self.assertEqual(12000, len(state['spec']))
        self.assertEqual(4000, len(state['check']))
        self.assertEqual('(untyped)', state['task_type'])
        question = ringer.jev_lane_question(self.lanes)['lane']
        self.assertEqual('choice', question['type'])
        self.assertEqual({name: lane.criteria for name, lane in self.lanes.items()}, question['criteria'])
        script = self.root / 'check script.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        task = replace(self.task, check="'check script.sh' --flag")
        self.assertEqual(task.check + '\n' + script.read_text(), ringer.jev_check_text(task, self.root))
        task = replace(task, check=str(script))
        self.assertEqual(task.check, ringer.jev_check_text(task, None))
        task = replace(task, check="'" + str(script) + "'")
        self.assertIn('exit 0', ringer.jev_check_text(task, None))
        self.assertEqual('true', ringer.jev_check_text(self.task, self.root))
        self.assertEqual("'bad", ringer.jev_check_text(replace(self.task, check="'bad"), self.root))

    def test_pick_and_failures(self):
        pick = ringer.jev_pick_lane(self.config, self.task, self.lanes)
        self.assertEqual(ringer.JevPick('codex-sol-medium', .91, 'codex', 'gpt-5.6-sol'), pick)
        source, state, questions, caller = json.loads(self.record.read_text())
        self.assertEqual('ringer-spec', source)
        self.assertEqual('ringer', caller)
        self.assertEqual(ringer.jev_lane_state(self.task, 'true'), state)
        self.assertEqual(ringer.jev_lane_question(self.lanes), questions)
        for answer in [None, {}, {'lane': {'choice': 'unknown'}}, {'lane': {'choice': 'codex-sol-medium', 'confidence': 'bad'}}]:
            with self.subTest(answer=answer):
                self.assertIsNone(ringer.jev_pick_lane(self.config, self.task, self.lanes, client=lambda *a, **k: answer))
        default = ringer.jev_pick_lane(self.config, self.task, self.lanes, client=lambda *a, **k: {'lane': {'choice': 'codex-sol-medium'}})
        self.assertEqual(0.0, default.confidence)
        self.assertIsNone(ringer.jev_pick_lane(replace(self.config, client=self.root / 'absent'), self.task, self.lanes))
        self.assertIsNone(ringer.jev_pick_lane(replace(self.config, enabled=False), self.task, self.lanes))
        self.assertIsNone(ringer.jev_pick_lane(self.config, self.task, {}))
        with patch.dict(os.environ, {'RINGER_NO_JEV': '1'}):
            self.assertIsNone(ringer.jev_pick_lane(self.config, self.task, self.lanes))
        self.client.write_text('raise RuntimeError("broken import")\n')
        self.assertIsNone(ringer.jev_pick_lane(self.config, self.task, self.lanes))
        def broken(*args, **kwargs):
            raise RuntimeError('broken ask')
        self.assertIsNone(ringer.jev_pick_lane(self.config, self.task, self.lanes, client=broken))

    def test_config(self):
        self.assertEqual(ringer.JevConfig(), ringer.load_jev_config('invalid'))
        config = ringer.load_jev_config({'enabled': False, 'client': str(self.client), 'lanes': str(self.lanes_path)})
        self.assertFalse(config.enabled)
        self.assertEqual(self.client.resolve(), config.client)
        self.assertEqual(self.lanes_path.resolve(), config.lanes)

    def fixture(self, missing=False, failing=False):
        log = self.root / 'runs.jsonl'
        config = self.root / 'config.toml'
        client = self.root / 'missing.py' if missing else self.client
        config.write_text(f'''
state_dir = {json.dumps(str(self.root / 'state'))}
[eval]
backend = "jsonl"
jsonl_path = {json.dumps(str(log))}
[artifact]
enabled = false
[jev]
client = {json.dumps(str(client))}
lanes = {json.dumps(str(self.lanes_path))}
[engines.mock]
bin = {json.dumps(sys.executable)}
args_template = [{json.dumps(str(ROOT / 'engines/mock_worker.py'))}, "{{spec}}"]
sandbox_args = []
full_access_args = []
''')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({
            'run_name': 'jev-shadow', 'workdir': str(self.root / 'work'), 'worktrees': False,
            'tasks': [{'key': 'one', 'engine': 'mock', 'task_type': 'docs',
                       'spec': 'Write the requested file in the current task directory. Follow the specified content exactly.\nMOCK_FILE: hello.txt\nhello\nMOCK_END' if not failing else 'Simulate a failure in the deterministic mock worker. Do not write output. MOCK_FAIL',
                       'check': "grep -q hello hello.txt || { echo 'FAIL: missing hello'; exit 1; }",
                       'expect_files': ['hello.txt']}]}
        ))
        return config, manifest, log

    def command(self, *args):
        env = os.environ.copy()
        env.update({'HOME': str(self.root), 'RINGER_HOME': str(self.root),
                    'XDG_CONFIG_HOME': str(self.root / 'xdg'), 'PYTHONDONTWRITEBYTECODE': '1'})
        return subprocess.run([sys.executable, str(ROOT / 'ringer.py'), *map(str, args)],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_mock_run_and_db_rebuild(self):
        config, manifest, log = self.fixture(failing=True)
        proc = self.command('run', manifest, '--config', config, '--no-dashboard', '--identity', 'shadow-test')
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len(self.record.read_text().splitlines()))
        self.assertEqual(1, proc.stdout.count('jev: codex-sol-medium'))
        for row in rows:
            self.assertEqual('mock', row['worker_engine'])
            self.assertEqual('codex-sol-medium', row['jev_pick'])
            self.assertEqual(.91, row['jev_confidence'])
            self.assertEqual('codex', row['jev_lane_engine'])
            self.assertEqual('gpt-5.6-sol', row['jev_lane_model'])
        states = list((self.root / 'state').rglob('*.json'))
        task_states = [json.loads(path.read_text()) for path in states]
        task_states = [state for state in task_states if isinstance(state, dict) and 'tasks' in state]
        self.assertTrue(task_states)
        self.assertEqual('codex-sol-medium', task_states[0]['tasks'][0]['jev_pick'])
        proc = self.command('db', '--config', config, 'rebuild')
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        dbs = list(self.root.glob('ringer.db'))
        self.assertTrue(dbs)
        with contextlib.closing(sqlite3.connect(dbs[0])) as conn:
            self.assertEqual(2, conn.execute('SELECT count(*) FROM attempts').fetchone()[0])

    def test_run_disabled(self):
        config, manifest, log = self.fixture()
        proc = self.command('run', manifest, '--config', config, '--no-dashboard', '--no-jev', '--identity', 'shadow-test')
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse(self.record.exists())
        self.assertIsNone(json.loads(log.read_text())['jev_pick'])

    def test_lint_clean_and_findings_keep_exit_code(self):
        config, manifest, _ = self.fixture()
        for findings in ([], ['one: fixture finding']):
            with self.subTest(findings=findings), patch.object(ringer, 'lint_manifest', return_value=findings):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = ringer.main(['--config', str(config), 'lint', str(manifest)])
                self.assertEqual(1 if findings else 0, result)
                self.assertIn('jev: one: suggests', output.getvalue())
                self.assertIn('est. no data', output.getvalue())
                if not findings:
                    self.assertIn('lint: clean (1 tasks)', output.getvalue())

    def test_missing_client_run(self):
        config, manifest, log = self.fixture(missing=True)
        proc = self.command('run', manifest, '--config', config, '--no-dashboard', '--identity', 'shadow-test')
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        row = json.loads(log.read_text())
        self.assertEqual('mock', row['worker_engine'])
        for key in ('jev_pick', 'jev_confidence', 'jev_lane_engine', 'jev_lane_model'):
            self.assertIsNone(row[key])

    def test_lint_suggestion_and_disabled(self):
        config, manifest, log = self.fixture()
        log.write_text(json.dumps({'run_id': 'historical', 'task_key': 'writing', 'worker_engine': 'codex',
                                   'model': 'gpt-5.6-sol', 'task_type': 'docs', 'worker_tokens': 123,
                                   'verdict': 'PASS'}) + '\n')
        proc = self.command('--config', config, 'lint', manifest)
        self.assertIn('jev: one: suggests codex-sol-medium', proc.stdout)
        self.assertIn('123 tokens for docs on that lane (n=1)', proc.stdout)
        disabled = self.command('--config', config, 'lint', manifest, '--no-jev')
        self.assertNotIn('jev:', disabled.stdout)
        self.assertEqual(proc.returncode, disabled.returncode)
        self.assertEqual(disabled.stdout, '\n'.join(line for line in proc.stdout.splitlines() if not line.startswith('jev:')) + '\n')
        with patch.dict(os.environ, {'RINGER_NO_JEV': '1'}):
            disabled = self.command('--config', config, 'lint', manifest)
        self.assertNotIn('jev:', disabled.stdout)


if __name__ == '__main__':
    unittest.main()
