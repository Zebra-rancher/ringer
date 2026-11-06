"""Approved routine-review routing must reach argv without overriding explicit choices."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ringer

ROOT = Path(__file__).resolve().parents[1]


class ReviewRoutingTests(unittest.TestCase):
    def task(self, **overrides):
        return ringer.TaskSpec.from_obj({
            'key': 'review', 'task_type': 'code-review',
            'spec': 'Review the named repository diff and write findings.md with concrete defects and evidence.',
            'check': "test -s findings.md || { echo 'FAIL: findings.md is missing'; exit 1; }",
            **overrides,
        })

    def command(self, task):
        engine = ringer.load_engines({})['codex']
        return ringer.build_worker_command(
            engine, taskdir=Path('/tmp/review'), spec=task.spec,
            full_access=False, model=task.model, engine_args=task.engine_args,
        )

    def test_registry_default_reaches_effective_command(self):
        task = self.task()
        command = self.command(task)
        self.assertEqual('gpt-6.1-sol', ringer.effective_model_from_command(command))
        self.assertEqual('medium', ringer.effective_reasoning_effort_from_command(command))
        self.assertEqual(1, command.count('-m'))
        # Historical picks retain their original model identity.
        lanes = ringer.load_lanes(ROOT / 'registry/lanes.toml')
        self.assertEqual('gpt-5.6-sol', lanes['codex-sol-medium'].model)
        self.assertIn('Hard, judgment-heavy', lanes['codex-astra'].criteria)

    def test_explicit_harder_review_preserves_model_and_effort(self):
        task = self.task(model='gpt-6-astra', engine_args=['-c', 'model_reasoning_effort=high'])
        command = self.command(task)
        self.assertEqual('gpt-6-astra', ringer.effective_model_from_command(command))
        self.assertEqual('high', ringer.effective_reasoning_effort_from_command(command))
        self.assertNotIn('model_reasoning_effort=medium', command)
        self.assertEqual((), self.task(model='gpt-6-astra').engine_args)

    def test_explicit_cli_routing_bypasses_whole_default(self):
        for args in [
            ['-m', 'custom-model'], ['--model=custom-model'], ['-mcustom-model'],
            ['-c', 'model="custom-model"'], ['--config=model="custom-model"'],
            ['-cmodel="custom-model"'],
            ['-c', 'model_reasoning_effort=high'], ['--config=model_reasoning_effort="high"'],
            ['-c', 'model_provider="custom"'], ['--profile', 'review'], ['-preview'],
        ]:
            with self.subTest(args=args):
                task = self.task(engine_args=args)
                self.assertEqual('', task.model)
                self.assertEqual(tuple(args), task.engine_args)

    def test_other_engines_and_task_types_unchanged(self):
        for engine in ['claude', 'opencode', 'mock']:
            with self.subTest(engine=engine):
                task = self.task(engine=engine)
                self.assertEqual('', task.model)
                self.assertEqual((), task.engine_args)
        for task_type in ['', 'code-feature', 'code-fix', 'docs', 'probe', 'research', 'test-hardening', 'format-conversion']:
            with self.subTest(task_type=task_type):
                task = self.task(task_type=task_type)
                self.assertEqual('', task.model)
                self.assertEqual((), task.engine_args)

    def test_unrelated_args_retained_and_whitespace_normalized(self):
        task = self.task(task_type=' code-review ', model=' ', engine_args=['--color', 'never'])
        self.assertEqual('gpt-6.1-sol', task.model)
        self.assertEqual(('-c', 'model_reasoning_effort=medium', '--color', 'never'), task.engine_args)

    def test_missing_registry_lane_refuses_instead_of_silently_inheriting(self):
        with patch.object(ringer, 'load_lanes', return_value={}):
            with self.assertRaisesRegex(ValueError, 'codex-review registry lane'):
                self.task()
            self.assertEqual('gpt-6-astra', self.task(model='gpt-6-astra').model)

    def test_cli_dry_run_composes_real_default_without_workers_or_jev(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            config.write_text(
                f'state_dir = {json.dumps(str(root / "state"))}\n'
                '[update]\nauto = false\n[jev]\nenabled = false\n'
                '[engines.codex]\n'
                f'bin = {json.dumps(sys.executable)}\n'
                'model_default = "gpt-6-astra"\n'
                '[artifact]\nenabled = false\n'
            )
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({
                'run_name': 'review-routing-proof', 'workdir': str(root / 'work'),
                'tasks': [{'key': 'review', 'task_type': 'code-review',
                           'spec': self.task().spec, 'check': self.task().check}],
            }))
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'ringer.py'), '--config', str(config),
                 'run', str(manifest), '--dry-run', '--no-dashboard', '--no-jev'],
                capture_output=True, text=True,
                env={**os.environ, 'RINGER_NO_SELF_UPDATE': '1', 'RINGER_NO_CATALOG_REFRESH': '1'},
            )
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            self.assertIn('DRY RUN', proc.stdout)
            self.assertIn('-m gpt-6.1-sol', proc.stdout)
            self.assertIn('model_reasoning_effort=medium', proc.stdout)
            self.assertNotIn('-m gpt-6-astra', proc.stdout)
            self.assertFalse((root / 'work' / 'review' / 'worker.log').exists())


if __name__ == '__main__':
    unittest.main()
