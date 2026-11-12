"""Keep collected material outside the distributable skill (including symlink aliases)."""
from pathlib import Path
import os

REPO_ROOT = Path(__file__).resolve().parents[1]


def external_path(value, root=None):
    root = Path(root or REPO_ROOT).resolve()
    lexical = Path(os.path.abspath(Path(value).expanduser()))
    resolved = lexical.resolve()
    for candidate in (lexical, resolved, *(parent.resolve() for parent in lexical.parents)):
        if candidate == root or root in candidate.parents:
            raise ValueError('Research/runtime data must be outside the skill repository')
    return resolved
