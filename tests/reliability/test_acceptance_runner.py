"""Dependency-free acceptance runner contracts; never import the application."""
import configparser
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ACCEPTANCE = ROOT / "tests" / "acceptance.sh"


class AcceptanceRunnerTests(unittest.TestCase):
    def invoke_stubbed(self, *args, code=0, extra_env=None):
        # Substitute only external executables: exercise the real bash entry point.
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            log = temp / 'calls.jsonl'
            for name in ('python', 'node', 'curl'):
                stub = temp / name
                stub.write_text(
                    f'#!{sys.executable}\n'
                    'import json, os, pathlib, sys\n'
                    'with open(os.environ["STUB_LOG"], "a") as f:\n'
                    '    f.write(json.dumps({"command": pathlib.Path(sys.argv[0]).name, '
                    '"args": sys.argv[1:], "env": dict(os.environ)}) + "\\n")\n'
                    'sys.exit(int(os.environ["STUB_EXIT"]))\n'
                )
                stub.chmod(0o755)
            env = dict(PATH=f'{temp}:/usr/bin:/bin', PYTHON=str(temp / 'python'),
                       STUB_LOG=str(log), STUB_EXIT=str(code),
                       MEM_ENV_FILE='/never/read/production.env',
                       MEM_DATABASE_URL='postgresql://production.invalid/db',
                       MEM_DATA_DIR='/never/write/production',
                       MEM_EMBED_API_KEY='inherited-secret',
                       MEM_PUBLIC_BASE='https://production.invalid')
            env.pop('MEM_TEST_DATABASE_URL', None)
            env.pop('MEM_ALLOW_LEGACY_WRITE', None)
            env.pop('MEM_TEST_API_KEY', None)
            env.update(extra_env or {})
            result = subprocess.run(['bash', str(ACCEPTANCE), *args], env=env,
                                    capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_default_isolated_unit_does_not_enable_database_integration(self):
        result, calls = self.invoke_stubbed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call['command'] for call in calls], ['python', 'node'])
        settings = calls[0]['env']
        self.assertNotIn('MEM_TEST_DATABASE_URL', settings)
        self.assertIn('127.0.0.1:1/memorys_test', settings['MEM_DATABASE_URL'])
        self.assertEqual(settings['MEM_EMBED_API_KEY'], '')
        self.assertNotIn('MEM_PUBLIC_BASE', settings)
        self.assertNotEqual(settings['MEM_ENV_FILE'], '/never/read/production.env')
        self.assertFalse(pathlib.Path(settings['MEM_ENV_FILE']).exists(), 'temporary sandbox not removed')
        self.assertIn('tests/reliability', calls[0]['args'])
        self.assertIn('not integration', calls[0]['args'])
        self.assertEqual(calls[1]['args'][0], '--test')

    def test_pytest_defaults_only_collect_reliability(self):
        config = configparser.ConfigParser()
        config.read(ROOT / 'pytest.ini')
        self.assertEqual(config.get('pytest', 'testpaths', fallback=''), 'tests/reliability')
        self.assertEqual(config.get('pytest', 'python_files', fallback=''), 'test_*.py')

    def test_offline_runner_and_ci_are_isolated(self):
        runner = ROOT / 'scripts/check_acceptance_runner.sh'
        self.assertTrue(runner.is_file(), 'missing dependency-free regression entry point')
        workflow = ROOT / '.github/workflows/reliability.yml'
        self.assertTrue(workflow.is_file(), 'missing isolated CI workflow')
        source = workflow.read_text()
        for required in ('pgvector/pgvector:', 'POSTGRES_DB: memorys_test',
                         'CREATE EXTENSION IF NOT EXISTS vector',
                         'requirements.lock', 'MEM_TEST_DATABASE_URL:',
                         'bash tests/acceptance.sh --integration'):
            self.assertIn(required, source)
        self.assertNotIn('tests/e2e_test.py', source)
        self.assertNotIn('public_mcp_test', source)

    def test_main_failure_is_not_hidden_by_summary_or_frontend(self):
        result, calls = self.invoke_stubbed(code=37)
        self.assertEqual(result.returncode, 37)
        self.assertEqual([call['command'] for call in calls], ['python'])

    def test_integration_requires_an_explicit_local_test_database(self):
        for url in ('', 'postgresql+asyncpg://u:p@production.invalid:5432/memorys_test',
                    'postgresql+asyncpg://u:p@localhost:5432/production'):
            with self.subTest(url=url):
                result, calls = self.invoke_stubbed('--integration', extra_env={'MEM_TEST_DATABASE_URL': url})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])
        url = 'postgresql+asyncpg://fixture:fixture@127.0.0.1:15432/memorys_test'
        result, calls = self.invoke_stubbed('--integration', extra_env={'MEM_TEST_DATABASE_URL': url})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0]['env']['MEM_TEST_DATABASE_URL'], url)
        self.assertNotIn('not integration', calls[0]['args'])

    def test_named_test_database_suffix_is_allowed(self):
        url = 'postgresql+asyncpg://fixture:fixture@127.0.0.1:15432/memorys_test_improvements'
        result, calls = self.invoke_stubbed('--integration', extra_env={'MEM_TEST_DATABASE_URL': url})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0]['env']['MEM_TEST_DATABASE_URL'], url)

    def test_public_readonly_never_invokes_legacy_tests(self):
        result, calls = self.invoke_stubbed('--public-readonly')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['command'] for call in calls], ['curl'] * 3)
        for call in calls:
            self.assertNotIn('-X', call['args'])
            self.assertNotIn('--location', call['args'])
            self.assertIn('--fail', call['args'])
        result, calls = self.invoke_stubbed('--public-readonly', code=22)
        self.assertEqual(result.returncode, 22)
        self.assertEqual(len(calls), 1)

    def test_legacy_requires_opt_in_and_explicit_key(self):
        for overrides in ({}, {'MEM_ALLOW_LEGACY_WRITE': '1'}):
            result, calls = self.invoke_stubbed('--legacy-write', extra_env=overrides)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, [])
        result, calls = self.invoke_stubbed('--legacy-write', extra_env={
            'MEM_ALLOW_LEGACY_WRITE': '1', 'MEM_TEST_API_KEY': 'stub-only-key'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0]['args'], ['tests/public_mcp_test.py', 'stub-only-key'])
        self.assertEqual(calls[0]['env']['MEM_MCP_URL'], 'https://production.invalid/mcp/')

    def test_default_runner_has_no_production_credentials_or_targets(self):
        source = ACCEPTANCE.read_text()
        for unsafe in ("/root/", "/opt/memorys", "https://repo.xlingo.fun", "mint_key", "upstream_token_test", "hermes mcp"):
            self.assertNotIn(unsafe, source)
        self.assertIn('tests/reliability', source)
        self.assertIn('--legacy-write', source)
        self.assertIn('--public-readonly', source)

    def test_run_propagates_child_exit_code(self):
        # Extract only the function: the historical script's top level is unsafe.
        source = ACCEPTANCE.read_text()
        function = re.search(r"(?ms)^run\(\) \{.*?^\}", source)
        if function is None:
            self.fail('acceptance runner must define run()')
        result = subprocess.run(
            ["bash", "-c", function.group() + '\nrun failing bash -c "exit 37"'],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
