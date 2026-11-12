#!/usr/bin/env python3
"""Run offline Python and Node suites against the pinned external OpenCLI package."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--opencli-root', required=True)
p.add_argument('--node', default='node')
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'scripts', '-p', 'test_*.py'], cwd=root, env=env, check=True)
for file in sorted((root / 'scripts').glob('test_*.mjs')):
    print('Node:', file.name, flush=True)
    args = [a.node, str(file)]
    if file.name == 'test_deep_comments.mjs': args.append(str(Path(a.opencli_root).resolve()))
    subprocess.run(args, cwd=root, env=env, check=True)
