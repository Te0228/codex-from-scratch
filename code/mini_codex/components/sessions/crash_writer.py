"""Controlled process crash at an explicit file/journal boundary, no network."""
import os
from pathlib import Path
import sys
if __package__:
    from .journal import Journal
else:
    from journal import Journal


def main(mode, directory):
    if mode not in {'after_file', 'after_commit'}:
        raise ValueError(mode)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'task.txt'
    target.write_text('before', encoding='utf-8')
    journal = Journal(directory / 'journal.sqlite')
    journal.append('tool_intent', {'call_id': 'patch-1', 'path': 'task.txt'})
    journal.flush()
    target.write_text('after', encoding='utf-8')
    if mode == 'after_file':
        os._exit(73)
    journal.append('tool_result', {'call_id': 'patch-1', 'status': 'succeeded'})
    journal.flush()
    os._exit(74)


if __name__ == '__main__':
    main(*sys.argv[1:])
