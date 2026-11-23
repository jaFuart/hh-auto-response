"""Fresh CLI setup with synthetic files, optionally through the real Docker entrypoint."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hh_auto.core.config import load_config
from hh_auto.ai.profile import load_candidate_profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--entrypoint', action='store_true')
    args = parser.parse_args()
    command = ['/entrypoint.sh', 'configure'] if args.entrypoint else [sys.executable, str(ROOT / 'scripts/configure.py')]
    scratch = ROOT / '.tmp'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hh-fresh-setup-', dir=scratch) as folder:
        base = Path(folder)
        assert base.resolve().is_relative_to(scratch.resolve())
        data = base / 'new candidate data'
        source = base / 'selected profile.toml'
        source.write_text('version = 1\nrole = "Developer"\nconfirmed_skills = ["Python"]\n'
                          '[[projects]]\nname = "Demo API"\ntechnologies = ["Python"]\n'
                          'tasks = ["Built a search API"]\n', encoding='utf-8')
        mapping = base / 'selected case map.md'
        mapping.write_text('### 1. Demo API\nImplemented search in a synthetic test case.\n', encoding='utf-8')

        def run(action, *extra, expected=0):
            result = subprocess.run([*command, action, '--data-dir', str(data), *extra],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            assert result.returncode == expected, f'{action}: {result.returncode}: {result.stdout} {result.stderr}'
            return result.stdout

        run('init', '--provider', 'openai', '--model', 'test-model', '--gender', 'female',
            '--profile', str(source), '--competency-map', str(mapping), '--resume', 'sample123')
        cfg = load_config(data / 'config.server.toml', check_search_url=False, upgrade=False, load_env=False)
        assert cfg.ai.candidate_gender == 'female' and cfg.ai.model == 'test-model'
        assert cfg.resume_id == 'sample123' and cfg.ai.only_robot
        assert not cfg.schedule.enabled and not cfg.watch.robot_replies
        profile = load_candidate_profile(cfg.ai)
        assert profile.confirmed_skills == ('Python',) and profile.projects[0].supplementary
        report = json.loads(run('doctor', '--json', expected=2))
        errors = {row['item'] for row in report['checks'] if row['status'] == 'error'}
        assert {'api_key', 'session'} <= errors

        key = 'offline-fixture-key'
        cookie = 'offline-fixture-cookie'
        (data / '.env').write_text('OPENAI_API_KEY=' + key + '\n', encoding='utf-8')
        (data / 'data/resume.txt').write_text('Synthetic private resume marker', encoding='utf-8')
        (data / 'data/resume.txt.id').write_text('sample123', encoding='utf-8')
        (data / 'data/hh-session.json').write_text(json.dumps({'cookies': [
            {'name': 'hhrole', 'value': 'applicant', 'domain': '.hh.ru', 'path': '/', 'expires': -1},
            {'name': 'test-session', 'value': cookie, 'domain': '.hh.ru', 'path': '/', 'expires': -1}], 'origins': []}), encoding='utf-8')

        def snapshot():
            return {str(path.relative_to(data)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in data.rglob('*') if path.is_file()}

        before = snapshot()
        output = run('doctor', '--json')
        assert json.loads(output)['ok']
        assert key not in output and cookie not in output and 'Synthetic private resume marker' not in output
        assert snapshot() == before
        run('init', expected=2)
        assert snapshot() == before
    print('PASS: fresh configuration, own profile/map, female gender, model/resume, read-only doctor, no sends.')


if __name__ == '__main__':
    main()
