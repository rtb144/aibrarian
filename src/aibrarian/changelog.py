"""Central change log + per-path locking for everything that moves or repoints files.

Usage::

    with changelog.change(db, "move", "media_file", old_path=src, new_path=dst,
                          actor="cli") as ch:
        do_the_filesystem_move()
        ch.add_undo(lambda: move_it_back())        # used if the DB step fails
        ch.repoint(src, dst)                         # media_file + dependent paths
        ch.sidecars([(old_srt, new_srt), ...])       # logged with the same txn_id

Order of events:

1. non-blocking Postgres advisory locks on the old/new path (and media id) - if another
   pipeline holds one, :class:`Busy` is raised and nothing is touched; a media row whose
   stage lease (``locked_by``/``locked_at``) is still fresh is also ``Busy``;
2. an ``aib.change_log`` row is committed with status ``pending``;
3. the caller's filesystem op runs inside the ``with`` block;
4. on success, one DB transaction cascades the path updates (``media_file.path``,
   ``transcript_lang.path``, ``file_hash.path``, open ``suggestion.source_path``) and marks
   the row ``committed``;
5. on an exception (or if step 4 fails) the registered undo callbacks run (reverse order);
   the row ends ``rolled_back`` when they all succeeded, else ``failed``. ``ch.fail(msg)``
   marks a refusal (nothing moved) as ``failed`` without raising.

A ``pending`` row older than a few minutes therefore means a crashed/half-done change;
the ``consistency`` flow reports those. Databases without the change-log methods (unit-test
fakes) degrade to plain ``update_path`` calls, so behaviour is unchanged there.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("aibrarian.changelog")

LEASE_MINUTES = 360  # Database.claim default lease


class Busy(RuntimeError):
    """Another pipeline holds the path/media lock or a fresh stage lease."""


def default_actor() -> tuple[str, str | None]:
    """('prefect:<flow name>', flow run id) inside a Prefect run, else ('cli', None)."""
    try:
        from prefect.runtime import flow_run

        if flow_run.id:
            return f"prefect:{flow_run.flow_name}", str(flow_run.id)
    except Exception:
        pass
    return "cli", None


def _actor_from_source(source: str | None) -> tuple[str | None, str | None]:
    """Existing call sites pass ``source`` = 'cli' | 'api' | 'prefect:<run id>'."""
    if not source:
        return None, None
    if source.startswith("prefect:"):
        a, r = default_actor()
        return (a if a.startswith("prefect:") else "prefect"), (r or source.split(":", 1)[1])
    return source, None


@dataclass
class Change:
    db: Any
    op: str
    entity: str
    old_path: str | None
    new_path: str | None
    txn_id: str
    actor: str | None = None
    run_id: str | None = None
    id: int | None = None
    entity_id: int | None = None
    status: str = "pending"
    error: str | None = None
    unmissing: bool = False
    _repoint: list[tuple[str, str]] = field(default_factory=list)
    _undo: list[Callable[[], Any]] = field(default_factory=list)
    _sidecars: list[tuple[str, str]] = field(default_factory=list)
    _failed: str | None = None
    _external: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def repoint(self, old: str | Path, new: str | Path) -> None:
        self._repoint.append((str(old), str(new)))

    def add_undo(self, fn: Callable[[], Any]) -> None:
        self._undo.append(fn)

    def sidecars(self, pairs: Sequence[tuple[str | Path, str | Path]]) -> None:
        self._sidecars += [(str(a), str(b)) for a, b in pairs]

    def committed_by_caller(self) -> None:
        """The caller already marked the row committed inside its own DB transaction
        (e.g. ``Database.quarantine_media(..., change_id=ch.id)``)."""
        self._external = True

    def fail(self, msg: str) -> None:
        """Refused / not done (nothing moved): mark failed on exit, no exception."""
        self._failed = msg

    def _run_undo(self) -> bool:
        ok = True
        for fn in reversed(self._undo):
            try:
                fn()
            except Exception as exc:  # keep going; report
                ok = False
                log.error("change rollback step failed", extra={"change_id": self.id,
                                                                  "error": str(exc)})
        return ok


def _supported(db: Any) -> bool:
    return db is not None and hasattr(db, "change_begin") and hasattr(db, "change_finish")


def _lease_busy(db: Any, media_ids: Sequence[int], lease_minutes: int) -> str | None:
    if db is None or not hasattr(db, "get_media"):
        return None
    now = datetime.now(UTC)
    for mid in media_ids:
        row = db.get_media(mid) or {}
        at = row.get("locked_at")
        if row.get("locked_by") and at and (now - at).total_seconds() < lease_minutes * 60:
            return f"media {mid} leased by {row['locked_by']}"
    return None


def record(db: Any, op: str, entity: str, *, old_path: Any = None, new_path: Any = None,
           status: str = "committed", entity_id: int | None = None, error: str | None = None,
           actor: str | None = None, run_id: str | None = None, txn_id: str | None = None,
           sha256: str | None = None, size_bytes: int | None = None,
           details: dict[str, Any] | None = None) -> int | None:
    """One-shot log row (for ops that are already complete or purely informational)."""
    if not _supported(db):
        return None
    if actor is None:
        actor, run_id = default_actor()
    try:
        return db.change_begin({
            "actor": actor, "run_id": run_id, "txn_id": txn_id or uuid.uuid4().hex, "op": op,
            "entity": entity, "entity_id": entity_id,
            "old_path": str(old_path) if old_path else None,
            "new_path": str(new_path) if new_path else None, "sha256": sha256,
            "size_bytes": size_bytes, "status": status, "error": error,
            "details": details or {}})
    except Exception as exc:  # the log must never break the operation it describes
        log.warning("change_log write failed", extra={"error": str(exc)})
        return None


@contextlib.contextmanager
def change(db: Any, op: str, entity: str, *, old_path: Any = None, new_path: Any = None,
           entity_id: int | None = None, actor: str | None = None, run_id: str | None = None,
           source: str | None = None, txn_id: str | None = None, sha256: str | None = None,
           size_bytes: int | None = None, details: dict[str, Any] | None = None,
           lock: bool = True, media_ids: Sequence[int] = (), extra_lock_paths: Sequence[Any] = (),
           lease_minutes: int = LEASE_MINUTES, check_lease: bool = True) -> Iterator[Change]:
    if actor is None and source:
        actor, run_id = _actor_from_source(source)
    if actor is None:
        actor, run_id = default_actor()
    old_s = str(old_path) if old_path else None
    new_s = str(new_path) if new_path else None
    ch = Change(db, op, entity, old_s, new_s, txn_id or uuid.uuid4().hex, actor, run_id,
                entity_id=entity_id, details=dict(details or {}))
    mids = [m for m in ([*media_ids] + ([entity_id] if entity == "media_file" and entity_id
                                        else [])) if m is not None]
    keys = [p for p in (old_s, new_s, *(str(x) for x in extra_lock_paths)) if p]
    locks = (db.path_locks(keys, mids) if lock and db is not None and hasattr(db, "path_locks")
             else contextlib.nullcontext(True))
    with locks as got:
        if not got:
            raise Busy(f"locked by another pipeline: {old_s or new_s}")
        why = _lease_busy(db, mids, lease_minutes) if check_lease else None
        if why:
            raise Busy(why)
        sup = _supported(db)
        if sup:
            ch.id = db.change_begin({
                "actor": actor, "run_id": run_id, "txn_id": ch.txn_id, "op": op,
                "entity": entity, "entity_id": entity_id, "old_path": old_s, "new_path": new_s,
                "sha256": sha256, "size_bytes": size_bytes, "status": "pending",
                "details": ch.details})
        try:
            yield ch
        except BaseException as exc:
            rolled = bool(ch._undo) and ch._run_undo()
            ch.status, ch.error = ("rolled_back" if rolled else "failed"), str(exc)[:4000]
            if sup:
                with contextlib.suppress(Exception):
                    db.change_finish(ch.id, ch.status, ch.error)
            raise
        if ch._failed is not None:
            ch.status, ch.error = "failed", ch._failed
            if sup:
                db.change_finish(ch.id, "failed", ch._failed, details=ch.details)
            return
        try:
            if sup and ch._external:
                pass  # already committed inside the caller's transaction
            elif sup:
                ch.entity_id = db.change_finish(ch.id, "committed", None, ch.entity_id,
                                                repoint=ch._repoint, unmissing=ch.unmissing,
                                                details=ch.details)
            else:
                for o, n in ch._repoint:
                    if db is not None and hasattr(db, "update_path"):
                        m = db.update_path(o, n)
                        ch.entity_id = ch.entity_id if ch.entity_id is not None else m
        except Exception as exc:  # DB step failed after the FS op: undo the FS op
            rolled = bool(ch._undo) and ch._run_undo()
            ch.status, ch.error = ("rolled_back" if rolled else "failed"), str(exc)[:4000]
            if sup:
                with contextlib.suppress(Exception):
                    db.change_finish(ch.id, ch.status, ch.error)
            raise
        ch.status = "committed"
        for a, b in ch._sidecars:
            record(db, op, "sidecar", old_path=a, new_path=b, actor=actor, run_id=run_id,
                   txn_id=ch.txn_id, entity_id=ch.entity_id)
