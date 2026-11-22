"""Static evidence viewer: one self-contained `index.html` with the results embedded.

The page renders recorded evidence only. It executes nothing, loads nothing from the network
(no CDN, no fonts, no analytics) and works from `file://` and GitHub Pages alike.
"""

from __future__ import annotations

import html
import json
from importlib import resources
from pathlib import Path
from typing import Any


def _embed(results: dict[str, Any]) -> str:
    text = json.dumps(results, separators=(',', ':'), default=str)
    # Keep the JSON inert inside <script type="application/json">.
    return text.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def banner_text(results: dict[str, Any]) -> str:
    env = results.get('environment', {})
    pk = env.get('packages', {})
    git = env.get('git') or {}
    commit = git.get('commit', '')[:12] or 'not recorded'
    if git.get('dirty'):
        commit += ' (uncommitted changes)'
    date = str(results.get('generated_at', ''))[:10]
    return (
        f'Recorded evidence from a real run on {date}: pydantic-ai-slim {pk.get("pydantic-ai-slim")}, '
        f'pydantic-ai-harness {pk.get("pydantic-ai-harness")}, Python {env.get("python")} on '
        f'{env.get("system")} {env.get("machine")}, lab commit {commit}. This page does not execute anything.'
    )


def render(results: dict[str, Any]) -> str:
    template = resources.files('crashlab.viewer').joinpath('template.html').read_text(encoding='utf-8')
    return (
        template.replace('__CRASHLAB_BANNER__', html.escape(banner_text(results)))
        .replace('__CRASHLAB_GENERATED__', html.escape(str(results.get('generated_at', ''))))
        .replace('__CRASHLAB_DATA__', _embed(results))
    )


def build_site(results: dict[str, Any], out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index = out / 'index.html'
    index.write_text(render(results), encoding='utf-8')
    (out / '.nojekyll').write_text('')
    return index
