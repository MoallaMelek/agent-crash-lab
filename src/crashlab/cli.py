"""`crashlab` command line.

    crashlab doctor                      versions, POSIX check, a one-cell smoke run
    crashlab list [--finding F] [--json] curated cells and the claim each one tests
    crashlab run CELL_ID [--set axis=value ...] [--keep-workdir] [--json]
    crashlab matrix [-j N] [--repeat N] [--full | --quick | --only ID ... | --finding F]
                    [--out DIR] [--check]
    crashlab report RESULTS_JSON --out DIR

Exit codes: 0 success (DUPLICATE and friends are findings, not failures); 1 `--check` found an
observed verdict that differs from the expected one, a non-reproducible cell, or a LAB_ERROR;
2 usage error; 3 unsupported platform.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from crashlab.config import CellConfig
from crashlab.runner import PlatformNotSupported, ensure_posix

EXIT_OK, EXIT_CHECK_FAILED, EXIT_USAGE, EXIT_PLATFORM = 0, 1, 2, 3

VERDICT_STYLE = {
    'OK': '32',
    'DUPLICATE': '31;1',
    'MISSING': '33;1',
    'STRANDED': '35;1',
    'AMBIGUOUS_RESOLVED': '36;1',
    'KILL_NOT_REACHED': '90',
    'NOT_APPLICABLE': '90',
    'LAB_ERROR': '41;97',
}


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.environ.get('NO_COLOR'):
        return text
    return f'\033[{code}m{text}\033[0m'


def _verdict(v: str) -> str:
    return _color(v, VERDICT_STYLE.get(v, '0'))


def format_event(e: dict[str, Any]) -> str:
    ev = e.get('event', '?')
    head = f'a{e.get("attempt")} #{e.get("seq", 0):02d} {e.get("t_ms", 0):>7.1f}ms  '
    detail = ''
    if ev == 'kill':
        return head + _color(
            f'KILL ({str(e.get("crash")).upper()}) at {e.get("point")}  {e.get("tool") or ""}', '41;97'
        )
    if ev in ('tool_started', 'tool_settled'):
        detail = f'{e.get("tool")} {e.get("tool_call_id")}'
    elif ev == 'effect_committed':
        detail = f'{e.get("tool")} {e.get("tool_call_id")} -> {e.get("effect_id")} {"(replayed)" if e.get("replayed") else "(new)"}'
    elif ev == 'model_invoked':
        detail = f'k={e.get("k")} sees={e.get("sees_returns")}'
    elif ev == 'model_responded':
        detail = f'k={e.get("k")} emits={e.get("emits")}'
    elif ev == 'node_finished':
        detail = f'{e.get("node")} -> {e.get("next")}'
    elif ev in ('history_saved', 'history_saved_on_error'):
        detail = f'{e.get("n_messages")} msgs, tail={e.get("tail")}'
    elif ev == 'snapshot_saved':
        detail = f'{e.get("run_id")} state={e.get("state")} step={e.get("step_index")} n={e.get("n_messages")}'
    elif ev == 'ledger_written':
        detail = f'{e.get("run_id")} {e.get("tool_call_id")} {e.get("status")}'
    elif ev == 'resume_planned':
        detail = (
            f'{e.get("mechanism")} from {e.get("history_source")}'
            f'{" FALLBACK restart" if e.get("fallback_restart") else ""}'
            f' calls={e.get("deferred_calls")} approvals={e.get("deferred_approvals")}'
        )
    elif ev == 'attempt_started':
        detail = f'{e.get("action")} pid={e.get("pid")}'
    elif ev in ('run_completed', 'run_failed'):
        detail = str(e.get('output') or e.get('error'))[:120]
    return head + f'{ev:<22} {detail}'


def print_record(rec: dict[str, Any]) -> None:
    print(f'cell    {rec["id"]}')
    if rec.get('claim'):
        print(f'claim   {rec["claim"]}')
    print(f'config  {rec.get("label")}')
    print()
    for e in rec.get('timeline', []):
        print('  ' + format_event(e))
    print()
    prov = rec.get('provider') or {}
    for row in prov.get('charges', []):
        print(
            f'  FakePay charge {row["id"]} order={row["order_id"]} key={row["idempotency_key"]} call_ref={row["call_ref"]} attempt={row["attempt"]}'
        )
    for row in prov.get('notifications', []):
        print(
            f'  FakePay notification {row["id"]} key={row["idempotency_key"]} call_ref={row["call_ref"]} attempt={row["attempt"]}'
        )
    step = rec.get('step_store')
    if step:
        for r in step['tool_effects']:
            print(f'  ledger {r["run_id"]} {r["tool_call_id"]} {r["status"]}')
        for s in step['snapshots']:
            print(f'  snapshot {s["run_id"]} step={s["step_index"]} {s["state"]} n={s["n_messages"]} tail={s["tail"]}')
    print()
    for c in rec.get('checks', []):
        print(f'  check {c["status"]:<4} {c["name"]}: {c["detail"]}')
    print()
    exp = rec.get('expected')
    print(f'verdict {_verdict(rec["verdict"])}  effects={rec["effect_counts"]}' + (f'  expected={exp}' if exp else ''))
    print(f'        {rec["explanation"]}')
    for r in rec.get('responsibility', []):
        print(f'  - [{r["category"]}] {r["factor"]}')
    if rec.get('workdir'):
        print(f'\nworkdir kept: {rec["workdir"]}')


def _parse_set(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if '=' not in item:
            raise ValueError(f'--set expects axis=value, got {item!r}')
        k, v = item.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def cmd_list(args: argparse.Namespace) -> int:
    from crashlab.matrix import CURATED, FINDINGS

    cells = [c for c in CURATED if not args.finding or c.finding == args.finding]
    if args.json:
        print(json.dumps([c.to_dict() for c in cells], indent=1))
        return EXIT_OK
    current = None
    for c in cells:
        if c.finding != current:
            current = c.finding
            print(f'\n{c.finding}: {FINDINGS[c.finding]}')
        print(f'  {c.id:<52} expect {c.expected:<18} {c.claim}')
    print(f'\n{len(cells)} cells')
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from crashlab.matrix import curated_by_id
    from crashlab.runner import run_cell

    cells = curated_by_id()
    overrides = _parse_set(args.set or [])
    if args.cell_id == 'custom':
        cfg = CellConfig()
        claim, expected, finding = 'custom cell', None, 'custom'
    elif args.cell_id in cells:
        c = cells[args.cell_id]
        cfg, claim, expected, finding = c.config, c.claim, c.expected, c.finding
    else:
        close = [cid for cid in cells if args.cell_id.lower() in cid.lower()]
        hint = f' Did you mean: {", ".join(close[:5])}?' if close else ' Run `crashlab list`.'
        print(f'unknown cell id {args.cell_id!r}.{hint}', file=sys.stderr)
        return EXIT_USAGE
    if overrides:
        cfg = CellConfig.from_dict({**cfg.to_dict(), **overrides})
        expected = None  # the curated expectation no longer applies
        claim += f' (overridden: {overrides})'
    rec = run_cell(args.cell_id, cfg, keep_workdir=args.keep_workdir, timeout=args.timeout)
    rec.update(claim=claim, expected=expected, finding=finding)
    if args.json:
        print(json.dumps(rec, indent=1, default=str))
    else:
        print_record(rec)
    if rec['verdict'] == 'LAB_ERROR':
        return EXIT_CHECK_FAILED
    if args.check and expected and rec['verdict'] != expected:
        return EXIT_CHECK_FAILED
    return EXIT_OK


def cmd_matrix(args: argparse.Namespace) -> int:
    from crashlab.evidence import build_results, write_evidence
    from crashlab.matrix import CURATED, QUICK_IDS, curated_by_id, full_product, run_matrix

    if args.full:
        cells = full_product()
        mode = 'full'
    elif args.only:
        by_id = curated_by_id()
        missing = [x for x in args.only if x not in by_id]
        if missing:
            print(f'unknown cell id(s): {missing}', file=sys.stderr)
            return EXIT_USAGE
        cells = [by_id[x] for x in args.only]
        mode = 'only'
    elif args.quick:
        by_id = curated_by_id()
        cells = [by_id[x] for x in QUICK_IDS]
        mode = 'quick'
    else:
        cells = [c for c in CURATED if not args.finding or c.finding == args.finding]
        mode = 'curated' if not args.finding else f'finding:{args.finding}'
    if args.repeat < 1 or args.jobs < 1:
        print('--repeat and -j must be >= 1', file=sys.stderr)
        return EXIT_USAGE

    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr, flush=True)

    t0 = time.monotonic()
    records = run_matrix(cells, jobs=args.jobs, repeat=args.repeat, timeout=args.timeout, progress=progress)
    duration = round(time.monotonic() - t0, 1)
    run_info = {
        'mode': mode,
        'repeat': args.repeat,
        'jobs': args.jobs,
        'duration_s': duration,
        'argv': ['crashlab', *sys.argv[1:]],
    }
    results = build_results(records, run_info=run_info)
    if args.out:
        rj, sm = write_evidence(results, args.out)
        print(f'wrote {rj} and {sm}', file=sys.stderr)
    differs = [r for r in records if r.get('expectation') == 'differs']
    unstable = [r for r in records if not r.get('reproducible')]
    errors = [r for r in records if r['verdict'] == 'LAB_ERROR']
    width = max((len(r['id']) for r in records), default=10)
    for r in records:
        flag = ''
        if r.get('expectation') == 'differs':
            flag = f'  <- expected {r["expected"]}'
        if not r.get('reproducible'):
            flag += '  <- NOT REPRODUCIBLE ' + str([x['verdict'] for x in r['repeats']])
        print(f'{r["id"]:<{width}}  {_verdict(r["verdict"]):<18}  {r["effect_counts"]}{flag}')
    s = results['summary']
    print(
        f'\n{s["cells"]} cells x {args.repeat} in {duration}s: {s["verdicts"]}; '
        f'expectations {s["expectations"]}; reproducible {s["reproducible"]}/{s["cells"]}'
    )
    if args.check and (differs or unstable or errors):
        print(
            f'CHECK FAILED: {len(differs)} differ from expectation, {len(unstable)} not reproducible, '
            f'{len(errors)} lab errors',
            file=sys.stderr,
        )
        return EXIT_CHECK_FAILED
    if errors:
        print(f'{len(errors)} LAB_ERROR cell(s); see results', file=sys.stderr)
        return EXIT_CHECK_FAILED
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from crashlab.evidence import load_results
    from crashlab.viewer import build_site

    try:
        results = load_results(args.results)
    except (OSError, ValueError) as e:
        print(f'cannot read {args.results}: {e}', file=sys.stderr)
        return EXIT_USAGE
    index = build_site(results, args.out)
    print(f'wrote {index}')
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from crashlab.evidence import environment

    env = environment()
    print(f'python               {env["python"]} ({env["implementation"]})')
    print(f'platform             {env["platform"]}')
    for k, v in env['packages'].items():
        print(f'{k:<20} {v}')
    git = env.get('git')
    print(
        f'lab git commit       {git["commit"][:12] + (" (dirty)" if git["dirty"] else "") if git else "not a git checkout"}'
    )
    pinned = {'pydantic-ai-slim': '2.71.0', 'pydantic-ai-harness': '0.71.0'}
    ok = True
    for name, want in pinned.items():
        if env['packages'].get(name) != want:
            print(f'WARNING: {name} is {env["packages"].get(name)}, the matrix expectations were recorded on {want}')
    try:
        ensure_posix()
        print('POSIX / SIGKILL      ok')
    except PlatformNotSupported as e:
        print(f'POSIX / SIGKILL      {e}')
        return EXIT_PLATFORM
    if not args.no_smoke:
        from crashlab.matrix import curated_by_id
        from crashlab.runner import run_cell

        c = curated_by_id()['H2-naive-history-duplicates']
        rec = run_cell(c.id, c.config)
        attempts = rec.get('attempts', [])
        rc = attempts[0]['returncode'] if attempts else None
        ok = rec['verdict'] == c.expected and rc == -9
        print(
            f'smoke run            {c.id}: attempt 1 rc={rc}, verdict {rec["verdict"]} ({"ok" if ok else "UNEXPECTED"})'
        )
    return EXIT_OK if ok else EXIT_CHECK_FAILED


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='crashlab',
        description='Reproducible SIGKILL crash matrix for side-effecting Pydantic AI tools.',
    )
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('list', help='list curated cells and their claims')
    s.add_argument('--finding', help='only this finding group (H, B, F1..F5, R, D, S)')
    s.add_argument('--json', action='store_true')
    s.set_defaults(func=cmd_list)

    s = sub.add_parser('run', help='run one cell and print its timeline and verdict')
    s.add_argument('cell_id', help="a curated cell id, or 'custom' (defaults + --set)")
    s.add_argument('--set', action='append', metavar='AXIS=VALUE', help='override an axis, e.g. --set key=business')
    s.add_argument('--keep-workdir', action='store_true', help='keep the temporary workdir for inspection')
    s.add_argument('--json', action='store_true', help='print the full record as JSON')
    s.add_argument('--check', action='store_true', help='exit 1 if the verdict differs from the expected one')
    s.add_argument('--timeout', type=float, default=60.0, help='per-subprocess timeout in seconds')
    s.set_defaults(func=cmd_run)

    s = sub.add_parser('matrix', help='run the curated matrix (or the full product)')
    s.add_argument('-j', '--jobs', type=int, default=max(1, (os.cpu_count() or 2)), help='cells in parallel')
    s.add_argument('--repeat', type=int, default=1, help='run each cell N times and check reproducibility')
    g = s.add_mutually_exclusive_group()
    g.add_argument('--full', action='store_true', help='full cartesian product (large, unchecked)')
    g.add_argument('--quick', action='store_true', help='a small representative subset')
    g.add_argument('--only', nargs='+', metavar='CELL_ID')
    g.add_argument('--finding', help='one finding group')
    s.add_argument('--out', help='write results.json and summary.md here')
    s.add_argument('--check', action='store_true', help='exit 1 on unexpected verdicts or non-reproducible cells')
    s.add_argument('--timeout', type=float, default=60.0)
    s.add_argument('-q', '--quiet', action='store_true')
    s.set_defaults(func=cmd_matrix)

    s = sub.add_parser('report', help='build the static evidence viewer from a results.json')
    s.add_argument('results')
    s.add_argument('--out', required=True)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser('doctor', help='check versions and platform; run one smoke cell')
    s.add_argument('--no-smoke', action='store_true')
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in ('run', 'matrix'):
        try:
            ensure_posix()
        except PlatformNotSupported as e:
            print(str(e), file=sys.stderr)
            return EXIT_PLATFORM
    try:
        rc: int = args.func(args)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    return rc


if __name__ == '__main__':
    sys.exit(main())
