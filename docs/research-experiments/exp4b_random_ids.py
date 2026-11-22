"""Experiment 4b: same as exp4 but the model mints a fresh tool_call_id per response (RANDOM_IDS=1),
as real providers do. Shows idempotency keyed on tool_call_id only survives resume paths that keep
the persisted ModelResponse (i.e. do not re-ask the model)."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from exp4_sigkill_matrix_lib import run_child, charges  # noqa
cells = [
    ('naive_iter', 'after_effect', 'idem', 'resume_default'),
    ('folded_iter', 'next_model_request', 'idem', 'resume_default'),
    ('step', 'after_effect', 'idem', 'resume_default'),          # no snapshot -> app restarts the run
    ('step_frontier', 'after_effect', 'idem', 'resume_default'),  # complete snapshot = prompt only -> model re-asked
    ('step_frontier', 'after_effect', 'idem', 'resume_interrupted'),  # interrupted snapshot keeps the response
]
for persistence, kill_at, tool_mode, phase in cells:
    wd = Path(tempfile.mkdtemp(prefix='crashlab-rand-'))
    env = {'KILL_AT': kill_at, 'RANDOM_IDS': '1', 'PYDANTIC_AI_NO_BANNER': '1'}
    run_child(wd, persistence, tool_mode, 'first', env, kill_marker='AT ')
    _, _, out = run_child(wd, persistence, tool_mode, phase, env, kill_marker=None)
    total = charges(wd)
    print(f"RANDOM_IDS {persistence:13} kill@{kill_at:18} tool={tool_mode} {phase:18} charges={total} -> {'OK' if total == 1 else 'DUPLICATE' if total > 1 else 'MISSING'}")
    for l in out:
        if l.startswith(('HISTORY_TAIL', 'UNRESOLVED', 'OUTPUT', 'RESTARTED', 'LOOKUP')):
            print('      ', l[:200])
