"""Offline regressions for waste prevention; real checks and disposable Git trees."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import ringer
from tests.test_baseline_mode import init_git_repo

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = tuple(r'"' + field + r'"\s*:\s*([0-9]+)' for field in (
    'input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens',
))


class ProcessWasteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        init_git_repo(self.repo)
        self.config = self.root / 'config.toml'
        self.config.write_text('\n'.join([
            f'state_dir = {json.dumps(str(self.root / "state"))}',
            '[eval]', 'backend = "jsonl"',
            f'jsonl_path = {json.dumps(str(self.root / "runs.jsonl"))}',
            '[artifact]', 'enabled = false',
            '[engines.mock]', f'bin = {json.dumps(sys.executable)}',
            f'args_template = [{json.dumps(str(ROOT / "engines/mock_worker.py"))}, "{{spec}}"]',
            'sandbox_args = []', 'full_access_args = []',
        ]))
        self.env = patch.dict(os.environ, {
            'HOME': str(self.root), 'RINGER_HOME': str(self.root / 'ringer-home'),
            'XDG_CONFIG_HOME': str(self.root / 'xdg'),
            'RINGER_NO_SELF_UPDATE': '1', 'RINGER_NO_JEV': '1',
            'RINGER_SKIP_PREFLIGHT': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manifest_path = self.root / 'manifest.json'
        self.obj = {
            'run_name': 'process-waste-test', 'workdir': str(self.root / 'work'),
            'worktrees': True, 'repo': str(self.repo), 'max_parallel': 1,
            'tasks': [{
                'key': 'one', 'engine': 'mock', 'max_attempts': 1,
                'spec': 'You are the deterministic mock worker. Write exactly the file below and keep all changes scoped to this task.\nMOCK_FILE: hello.txt\nhello\nMOCK_END',
                'check': 'cat hello.txt || { echo "FAIL: missing file"; exit 1; }',
                'verified': 'hello.txt is readable', 'task_type': 'code-feature',
            }],
        }

    def invoke(self, *flags, real_run=False):
        self.manifest_path.write_text(json.dumps(self.obj))
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            stack.enter_context(patch.object(ringer, 'start_catalog_auto_refresh'))
            stack.enter_context(patch.object(ringer, 'print_steering_notes'))
            run = None if real_run else stack.enter_context(patch.object(ringer, 'run_manifest', new_callable=AsyncMock, return_value=0))
            rc = ringer.main(['run', str(self.manifest_path), '--config', str(self.config),
                              '--identity', 'test', '--no-dashboard', *flags])
        return rc, output.getvalue(), run

    def assert_aborted(self, check):
        self.obj['tasks'][0]['check'] = check
        rc, output, run = self.invoke()
        self.assertEqual(rc, 2, output)
        run.assert_not_awaited()
        self.assertIn('BROKEN CHECK', output)
        self.assertIn('no workers were spawned.', output)
        self.assertFalse((self.root / 'runs.jsonl').exists())
        self.assertFalse((self.root / 'work' / 'one').exists())
        listed = subprocess.check_output(['git', '-C', str(self.repo), 'worktree', 'list', '--porcelain'], text=True)
        self.assertEqual(listed.count('worktree '), 1)
        return output

    def test_usage_aborts_without_workers_or_eval(self):
        self.assert_aborted("printf 'usage: verify.py --required\\n'; exit 2")

    def test_traceback_aborts_without_workers_or_eval(self):
        self.assert_aborted("printf 'Traceback (most recent call last):\\n'; exit 1")

    def test_timeout_aborts_without_workers_or_eval(self):
        with patch.object(ringer, 'CHECK_TIMEOUT_S', 0.05):
            output = self.assert_aborted('sleep 70')
        self.assertIn('timed out after 60s', output)

    def test_missing_file_continues(self):
        rc, output, run = self.invoke()
        self.assertEqual(rc, 0, output)
        run.assert_awaited_once()
        self.assertIn('preflight: 1 check(s) executed on the clean tree; 1 expected-fail, 0 already-pass, 0 broken.', output)

    def test_already_pass_warning(self):
        self.obj['tasks'][0]['check'] = 'test -f README.md'
        rc, output, run = self.invoke()
        self.assertEqual(rc, 0, output)
        self.assertIn('0 expected-fail, 1 already-pass, 0 broken.', output)
        self.assertIn('check already passes with no worker — it cannot verify the work.', output)

    def test_skip_flag_and_environment(self):
        for flags, env in [(('--skip-preflight',), '0'), ((), '1')]:
            with self.subTest(flags=flags, env=env), patch.dict(os.environ, RINGER_SKIP_PREFLIGHT=env), patch.object(ringer, 'execute_checks_against_clean_tree', new_callable=AsyncMock) as checks:
                rc, output, run = self.invoke(*flags)
                self.assertEqual(rc, 0, output)
                checks.assert_not_awaited()
                run.assert_awaited_once()
                self.assertNotIn('preflight:', output)

    def test_non_worktrees_and_dry_run_skip(self):
        for worktrees, flags in [(False, ()), (True, ('--dry-run',))]:
            with self.subTest(worktrees=worktrees), patch.object(ringer, 'execute_checks_against_clean_tree', new_callable=AsyncMock) as checks:
                self.obj['worktrees'] = worktrees
                rc, output, run = self.invoke(*flags)
                self.assertEqual(rc, 0, output)
                checks.assert_not_awaited()

    def test_classify_markers_is_deliberately_small_and_case_sensitive(self):
        for excerpt in ['usage: check', 'prefix\nusage: check', 'syntax error', 'command not found',
                        'Traceback (most recent call last):', 'FATAL: bad', 'panic: bad']:
            with self.subTest(excerpt=excerpt):
                self.assertEqual(ringer.classify_preflight_failure(ringer.PreflightResult('one', False, 1, False, excerpt)), 'broken-check')
        for excerpt in ['No such file', 'Permission denied', 'AssertionError', 'fatal', 'PANIC', 'panicky', 'FATALITY', 'prefix usage: x']:
            with self.subTest(excerpt=excerpt):
                self.assertEqual(ringer.classify_preflight_failure(ringer.PreflightResult('one', False, 1, False, excerpt)), 'expected')
        self.assertEqual(ringer.classify_preflight_failure(ringer.PreflightResult('one', False, -15, True, '')), 'broken-check')

    def test_stale_registered_worktree_recovers_and_worker_passes(self):
        taskdir = self.root / 'work' / 'one'
        taskdir.parent.mkdir()
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(taskdir)], check=True, capture_output=True)
        (taskdir / 'stale.txt').write_text('previous run')
        rc, output, _ = self.invoke(real_run=True)
        self.assertEqual(rc, 0, output)
        self.assertRegex(output, r'one\s+pass\s+PASS')
        logs = ''.join(p.read_text() for p in (self.root / 'work').rglob('*.log'))
        self.assertIn(f'[ringer.py] removed stale worktree left by a previous run: {taskdir}', logs)
        self.assertTrue((self.root / 'runs.jsonl').exists())

    def test_locked_worktree_removal_failure_keeps_refusal(self):
        taskdir = self.root / 'work' / 'one'
        taskdir.parent.mkdir()
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(taskdir)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'lock', str(taskdir)], check=True, capture_output=True)
        rc, output, _ = self.invoke(real_run=True)
        self.assertNotEqual(rc, 0, output)
        self.assertIn('worktree taskdir already exists (left by a previous failed run?):', output)
        self.assertIn(f'git -C {self.repo} worktree remove --force {taskdir}', output)
        self.assertTrue((taskdir / '.git').is_file())

    def test_unregistered_git_file_keeps_refusal(self):
        taskdir = self.root / 'work' / 'one'
        taskdir.mkdir(parents=True)
        (taskdir / '.git').write_text('gitdir: /does/not/exist\n')
        rc, output, _ = self.invoke(real_run=True)
        self.assertNotEqual(rc, 0, output)
        self.assertIn('worktree taskdir already exists (left by a previous failed run?):', output)
        self.assertIn(f'git -C {self.repo} worktree remove --force {taskdir}', output)
        self.assertTrue((taskdir / '.git').is_file())

    def test_spec_advisories_do_not_change_lint_exit(self):
        for size in (6000, 6001):
            with self.subTest(size=size):
                self.obj['tasks'][0]['spec'] = 'x' * size
                manifest = ringer.Manifest.from_obj(self.obj)
                advisories = []
                self.assertEqual(ringer.lint_manifest(manifest, advisories=advisories), [])
                self.assertEqual(len(advisories), int(size > 6000))
                self.manifest_path.write_text(json.dumps(self.obj))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = ringer.main(['lint', str(self.manifest_path), '--no-jev'])
                self.assertEqual(rc, 0, output.getvalue())
                self.assertEqual('lint (advisory):' in output.getvalue(), size > 6000)
                if size > 6000:
                    self.assertIn('one: spec is 6001 chars;', output.getvalue())
                    rc, run_output, _ = self.invoke()
                    self.assertEqual(rc, 0, run_output)
                    self.assertIn('lint (advisory): one: spec is 6001 chars;', run_output)

    def test_token_regexes_sum_last_match_of_each_field(self):
        engine = ringer.load_engines({'claude': {'args_template': ['{spec}'], 'token_regexes': list(PATTERNS)}})['claude']
        text = '{"input_tokens": 1, "output_tokens": 2}\n' + json.dumps({'usage': {
            'input_tokens': 10000, 'cache_creation_input_tokens': 7000,
            'cache_read_input_tokens': 500000, 'output_tokens': 5772,
        }})
        self.assertEqual(ringer.parse_token_count(text, engine.token_regex, engine.token_regexes), 522772)
        self.assertIsNone(ringer.parse_token_count('no usage', token_regexes=PATTERNS))
        self.assertEqual(ringer.parse_token_count('{"output_tokens": 0}', token_regexes=PATTERNS), 0)
        self.assertEqual(ringer.parse_token_count('{"output_tokens": 42}', token_regexes=PATTERNS), 42)
        self.assertEqual(ringer.parse_token_count('tokens used: 123'), 123)

    def test_invalid_token_regex_names_engine(self):
        self.config.write_text('[engines.claude]\nargs_template = ["{spec}"]\ntoken_regexes = ["["]\n')
        with self.assertRaisesRegex(ValueError, r'engines\.claude\.token_regexes'):
            ringer.AppConfig.load(self.config)


if __name__ == '__main__':
    unittest.main()
