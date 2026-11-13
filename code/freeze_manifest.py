"""Maintainer-only: freeze reviewed verifier inputs and trusted Python sources.

Never invoke this from a task or a test to repair a hash mismatch.
"""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
assets = root / 'mini_codex/assets'
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
cases = json.loads((assets / 'checks/cases.json').read_text())
protected = {str(p.relative_to(root)): sha(p) for p in sorted((root / 'mini_codex').rglob('*.py'))
             if '__pycache__' not in p.parts and not p.is_relative_to(assets)}
protected.update({name: sha(assets / name) for name in
                  ('checks/cases.json', 'fixtures/task-list/task_list.py')})
manifest = {'schema': 'mini-codex-fixed-verification-v1',
    'case_ids': [c['id'] for c in cases], 'expected_count': len(cases),
    'red_witness_ids': [c['id'] for c in cases if c['red']],
    'baseline_sha256': sha(assets / 'fixtures/task-list/task_list.py'),
    'protected_hashes': protected}
(assets / 'checks/manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Frozen {len(cases)} cases and {len(protected)} trusted paths')
