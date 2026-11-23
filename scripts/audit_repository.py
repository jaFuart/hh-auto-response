"""Check a publishable source tree and optional Git history without printing secrets."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess

TOKEN_PATTERNS = {
    'OpenAI key': re.compile(rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'AWS access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'Slack token': re.compile(rb'\bxox[baprs]-[A-Za-z0-9-]{20,}'),
    'private key': re.compile(rb'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----'),
    'credential in URL': re.compile(rb'https?://[^\s/<>:]{2,}:[^\s/<>@]{8,}@'),
}
PRIVATE_NAMES = {'candidate_profile_hh.json', 'competency_map_maxim.md', 'hh_bot_codex_task.md',
                 'hh-session.json', 'config.toml', 'config.server.toml', 'cookies', 'cookies.sqlite',
                 'chat_answers.jsonl', 'responses.jsonl', 'resume.txt', 'resume.txt.id'}
PRIVATE_PARTS = {'hh-data', 'browser-profile', 'browser-profile-server', '.ssh', '.venv', '.tmp', 'logs'}


def private_path(name: str) -> bool:
    path = PurePosixPath(name.replace('\\', '/'))
    return (bool(PRIVATE_PARTS.intersection(path.parts)) or path.name.casefold() in PRIVATE_NAMES
            or path.name.startswith('.env') and path.name != '.env.example'
            or path.suffix.casefold() in {'.pem', '.key', '.p12', '.pfx', '.zip', '.gz', '.jsonl', '.log'})


def inspect(name: str, content: bytes) -> list[str]:
    result = [f'private path: {name}'] if private_path(name) else []
    for label, pattern in TOKEN_PATTERNS.items():
        if pattern.search(content):
            result.append(f'{label}: {name}')
    return result


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError('Git history could not be checked.')
    return result.stdout


def audit(root: Path, *, history: bool = False) -> tuple[int, int, list[str]]:
    files = 0
    issues = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if '.git' in relative.parts or '__pycache__' in relative.parts or not path.is_file():
            continue
        if path.is_symlink():
            issues.append(f'symlink: {relative.as_posix()}')
            continue
        files += 1
        issues.extend(inspect(relative.as_posix(), path.read_bytes()))
    commits = 0
    if history:
        refs = git(root, 'rev-list', '--all').decode().splitlines()
        commits = len(refs)
        seen = set()
        for commit in refs:
            entries = git(root, 'ls-tree', '-rz', commit).split(b'\0')
            for entry in entries:
                if not entry:
                    continue
                metadata, name = entry.split(b'\t', 1)
                mode, kind, sha = metadata.split()
                label = name.decode('utf-8', errors='replace')
                if private_path(label):
                    issues.append(f'private path in history: {label}')
                if kind != b'blob' or sha in seen:
                    continue
                seen.add(sha)
                issues.extend(inspect(label, git(root, 'cat-file', 'blob', sha.decode())))
    return files, commits, sorted(set(issues))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--git-history', action='store_true')
    args = parser.parse_args(argv)
    try:
        files, commits, issues = audit(args.path.resolve(), history=args.git_history)
    except (OSError, ValueError) as error:
        parser.exit(2, f'Cannot complete audit: {error}\n')
    print(f'Checked {files} files and {commits} commits; findings: {len(issues)}.')
    for issue in issues:
        print(issue)
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
