"""FakePay: a simulated external system and the lab's only source of truth for effects.

FakePay stands in for a payment API (charges) and a messaging API (notifications). It lives in
its own SQLite file, separate from anything the agent or its persistence layer writes, so a
crash of the agent process cannot corrupt the ground truth the oracle counts.

Semantics (Stripe-like, deliberately simple):

* `charge(order_id, amount_cents, idempotency_key=None)` creates a charge. If a charge with the
  same non-null `idempotency_key` exists, nothing is created and the *original* charge is returned
  with `replayed=True`. Without a key every call creates a new charge.
* `call_ref` is free-form metadata (the lab stores the `tool_call_id` there), searchable the way
  Stripe `metadata` is. It is **not** a dedupe key.
* Each write is one `BEGIN IMMEDIATE` transaction, committed before the method returns, with
  `journal_mode=WAL` and `synchronous=FULL`.

Guarantees: a committed charge survives a `SIGKILL` of the calling process (the OS keeps the
page cache). Non-guarantees: nothing here says anything about power loss or torn writes, and the
idempotency check is per database file only. No real money moves and no message is sent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    idempotency_key TEXT UNIQUE,
    call_ref TEXT,
    created_at_seq INTEGER NOT NULL,
    attempt INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_charges_order ON charges(order_id);
CREATE INDEX IF NOT EXISTS idx_charges_call_ref ON charges(call_ref);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    message TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    call_ref TEXT,
    created_at_seq INTEGER NOT NULL,
    attempt INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_order ON notifications(order_id);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    idempotency_key TEXT,
    call_ref TEXT,
    attempt INTEGER NOT NULL
);
"""

EffectKind = str  # 'charge' | 'notification'


@dataclass(frozen=True)
class ChargeResult:
    """What FakePay returns for a charge request."""

    charge_id: str
    order_id: str
    amount_cents: int
    idempotency_key: str | None
    call_ref: str | None
    created_at_seq: int
    attempt: int
    replayed: bool
    """True when an existing charge was returned for a repeated idempotency key."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NotificationResult:
    """What FakePay returns for a notification request."""

    notification_id: str
    order_id: str
    message: str
    idempotency_key: str | None
    call_ref: str | None
    created_at_seq: int
    attempt: int
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FakePay:
    """Connection-per-call client for a FakePay database file.

    `attempt` is lab metadata: the process attempt that made the request, recorded on each row so
    evidence can show which attempt produced which effect. A real provider would not know it.
    """

    def __init__(self, path: str | Path, *, attempt: int = 0) -> None:
        self.path = Path(path)
        self.attempt = attempt
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- charges

    def charge(
        self,
        order_id: str,
        amount_cents: int,
        idempotency_key: str | None = None,
        *,
        call_ref: str | None = None,
    ) -> ChargeResult:
        """Create a charge, or return the existing one for a repeated `idempotency_key`."""
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                if idempotency_key is not None:
                    row = conn.execute('SELECT * FROM charges WHERE idempotency_key = ?', (idempotency_key,)).fetchone()
                    if row is not None:
                        conn.execute(
                            'INSERT INTO events (kind, ref_id, order_id, idempotency_key, call_ref, attempt) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            ('charge_replayed', row['id'], order_id, idempotency_key, call_ref, self.attempt),
                        )
                        conn.execute('COMMIT')
                        return _charge_from_row(row, replayed=True)
                cur = conn.execute(
                    'INSERT INTO events (kind, ref_id, order_id, idempotency_key, call_ref, attempt) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    ('charge_created', '', order_id, idempotency_key, call_ref, self.attempt),
                )
                seq = int(cur.lastrowid or 0)
                n = int(conn.execute('SELECT COUNT(*) FROM charges').fetchone()[0]) + 1
                charge_id = f'ch_{n:04d}'
                conn.execute('UPDATE events SET ref_id = ? WHERE seq = ?', (charge_id, seq))
                conn.execute(
                    'INSERT INTO charges (id, order_id, amount_cents, idempotency_key, call_ref, created_at_seq, attempt) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (charge_id, order_id, amount_cents, idempotency_key, call_ref, seq, self.attempt),
                )
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
            return ChargeResult(
                charge_id=charge_id,
                order_id=order_id,
                amount_cents=amount_cents,
                idempotency_key=idempotency_key,
                call_ref=call_ref,
                created_at_seq=seq,
                attempt=self.attempt,
                replayed=False,
            )
        finally:
            conn.close()

    def find_by_key(self, idempotency_key: str) -> ChargeResult | None:
        """The charge created with `idempotency_key`, if any (a read; no event is logged)."""
        return self._one_charge('SELECT * FROM charges WHERE idempotency_key = ?', (idempotency_key,))

    def find_by_call_ref(self, call_ref: str) -> ChargeResult | None:
        """The first charge whose metadata `call_ref` matches (metadata search, not dedupe)."""
        return self._one_charge('SELECT * FROM charges WHERE call_ref = ? ORDER BY created_at_seq', (call_ref,))

    def find_by_order(self, order_id: str) -> list[ChargeResult]:
        """All charges for `order_id`, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                'SELECT * FROM charges WHERE order_id = ? ORDER BY created_at_seq', (order_id,)
            ).fetchall()
        finally:
            conn.close()
        return [_charge_from_row(r, replayed=False) for r in rows]

    def count(self, order_id: str) -> int:
        """Number of charges for `order_id`. The oracle's ground truth."""
        return len(self.find_by_order(order_id))

    def _one_charge(self, sql: str, params: tuple[Any, ...]) -> ChargeResult | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return None if row is None else _charge_from_row(row, replayed=False)

    # ----------------------------------------------------------- notifications

    def notify(
        self,
        order_id: str,
        message: str,
        idempotency_key: str | None = None,
        *,
        call_ref: str | None = None,
    ) -> NotificationResult:
        """Record a customer notification, with the same key semantics as `charge`."""
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                if idempotency_key is not None:
                    row = conn.execute(
                        'SELECT * FROM notifications WHERE idempotency_key = ?', (idempotency_key,)
                    ).fetchone()
                    if row is not None:
                        conn.execute(
                            'INSERT INTO events (kind, ref_id, order_id, idempotency_key, call_ref, attempt) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            ('notification_replayed', row['id'], order_id, idempotency_key, call_ref, self.attempt),
                        )
                        conn.execute('COMMIT')
                        return _notification_from_row(row, replayed=True)
                cur = conn.execute(
                    'INSERT INTO events (kind, ref_id, order_id, idempotency_key, call_ref, attempt) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    ('notification_created', '', order_id, idempotency_key, call_ref, self.attempt),
                )
                seq = int(cur.lastrowid or 0)
                n = int(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]) + 1
                notification_id = f'nt_{n:04d}'
                conn.execute('UPDATE events SET ref_id = ? WHERE seq = ?', (notification_id, seq))
                conn.execute(
                    'INSERT INTO notifications (id, order_id, message, idempotency_key, call_ref, created_at_seq, attempt) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (notification_id, order_id, message, idempotency_key, call_ref, seq, self.attempt),
                )
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
            return NotificationResult(
                notification_id=notification_id,
                order_id=order_id,
                message=message,
                idempotency_key=idempotency_key,
                call_ref=call_ref,
                created_at_seq=seq,
                attempt=self.attempt,
                replayed=False,
            )
        finally:
            conn.close()

    def find_notification_by_key(self, idempotency_key: str) -> NotificationResult | None:
        return self._one_notification('SELECT * FROM notifications WHERE idempotency_key = ?', (idempotency_key,))

    def find_notification_by_call_ref(self, call_ref: str) -> NotificationResult | None:
        return self._one_notification(
            'SELECT * FROM notifications WHERE call_ref = ? ORDER BY created_at_seq', (call_ref,)
        )

    def find_notifications_by_order(self, order_id: str) -> list[NotificationResult]:
        conn = self._connect()
        try:
            rows = conn.execute(
                'SELECT * FROM notifications WHERE order_id = ? ORDER BY created_at_seq', (order_id,)
            ).fetchall()
        finally:
            conn.close()
        return [_notification_from_row(r, replayed=False) for r in rows]

    def count_notifications(self, order_id: str) -> int:
        return len(self.find_notifications_by_order(order_id))

    def _one_notification(self, sql: str, params: tuple[Any, ...]) -> NotificationResult | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return None if row is None else _notification_from_row(row, replayed=False)

    # ---------------------------------------------------------------- evidence

    def dump(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of every table, for evidence. Read-only."""
        conn = self._connect()
        try:
            out: dict[str, list[dict[str, Any]]] = {}
            for table, order in (('charges', 'created_at_seq'), ('notifications', 'created_at_seq'), ('events', 'seq')):
                out[table] = [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY {order}').fetchall()]
            return out
        finally:
            conn.close()


def _charge_from_row(row: sqlite3.Row, *, replayed: bool) -> ChargeResult:
    return ChargeResult(
        charge_id=row['id'],
        order_id=row['order_id'],
        amount_cents=row['amount_cents'],
        idempotency_key=row['idempotency_key'],
        call_ref=row['call_ref'],
        created_at_seq=row['created_at_seq'],
        attempt=row['attempt'],
        replayed=replayed,
    )


def _notification_from_row(row: sqlite3.Row, *, replayed: bool) -> NotificationResult:
    return NotificationResult(
        notification_id=row['id'],
        order_id=row['order_id'],
        message=row['message'],
        idempotency_key=row['idempotency_key'],
        call_ref=row['call_ref'],
        created_at_seq=row['created_at_seq'],
        attempt=row['attempt'],
        replayed=replayed,
    )
