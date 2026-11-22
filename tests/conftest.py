from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

from crashlab.provider import FakePay


@pytest.fixture
def fakepay(tmp_path: Path) -> FakePay:
    return FakePay(tmp_path / 'fakepay.db', attempt=1)
