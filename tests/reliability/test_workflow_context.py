"""Guard the job-level context error that prevents GitHub from starting CI."""
from pathlib import Path
import re
import yaml


def test_job_env_uses_only_contexts_available_before_runner_allocation():
    root = Path(__file__).resolve().parents[2]
    allowed = {'github', 'needs', 'strategy', 'matrix', 'vars', 'secrets', 'inputs'}
    for workflow in (root / '.github' / 'workflows').glob('*.yml'):
        doc = yaml.safe_load(workflow.read_text())
        for name, job in doc.get('jobs', {}).items():
            for key, value in job.get('env', {}).items():
                for expr in re.findall(r'\$\{\{(.*?)\}\}', str(value)):
                    contexts = set(re.findall(r'\b([a-zA-Z_]+)\s*\.', expr))
                    assert contexts <= allowed, (
                        f'{workflow.name}: jobs.{name}.env.{key}: '
                        f'contexts unavailable before runner allocation: {contexts - allowed}'
                    )
