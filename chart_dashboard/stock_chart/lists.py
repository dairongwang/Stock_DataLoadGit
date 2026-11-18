"""
The stock lists of the chart dashboard, saved in the database:

    watch     the watchlist (one per owner) - seeded from ``[chart] watchlist`` on first use
    selected  the stocks picked while reviewing charts (one per owner; "Add to Selected")
    screen    lists loaded from text files (any number; symbols separated by commas)

Tables (created on first use; reference script ``sql/create_chart_lists_sqlite.sql``)::

    Chart_List       ListId, Kind, Name, Owner, SourceFile, Revision, CreatedAt, UpdatedAt
    Chart_List_Item  ListId, Position, Ticker, AddedAt     (one row per ticker of a list, in list order)

By default they live in the pipeline's database (``[database] sqlite_path``) next to the stock
tables; ``[lists] sqlite_path`` / ``$STOCK_CHART_LISTS_DB`` put them into a file of their own.
This module is the ONLY place the dashboard writes - and it only writes the two ``Chart_*``
tables; the stock tables are read through read-only connections (``repository.py``).

``Owner`` is the signed-in user of ``[auth] mode = basic`` (empty without authentication: the
lists are shared by everybody who opens the dashboard).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

log = logging.getLogger(__name__)

LIST_TABLE = "Chart_List"
ITEM_TABLE = "Chart_List_Item"

KIND_WATCH = "watch"
KIND_SELECTED = "selected"
KIND_SCREEN = "screen"
KINDS = (KIND_WATCH, KIND_SELECTED, KIND_SCREEN)
#: the lists every owner has (created on first use)
DEFAULT_NAMES = {KIND_WATCH: "Watchlist", KIND_SELECTED: "Selected"}

#: most tickers one list holds
LIST_MAX = 1000
#: most screen lists one owner keeps
SCREEN_LISTS_MAX = 50
#: the largest screen-list file accepted
SCREEN_FILE_MAX_BYTES = 1024 * 1024

TICKER_RE = re.compile(r"^[A-Z0-9.\-^=_]{1,20}$")
_SPLIT_RE = re.compile(r"[,;\s]+")
_NAME_RE = re.compile(r"[^\w .\-()]+")

LIST_DDL = f"""
CREATE TABLE IF NOT EXISTS "{LIST_TABLE}" (
    "ListId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "Kind"        TEXT NOT NULL,                -- watch | selected | screen
    "Name"        TEXT NOT NULL,
    "Owner"       TEXT NOT NULL DEFAULT '',     -- the basic-auth user ('' = shared)
    "SourceFile"  TEXT,                         -- screen lists: the file they were loaded from
    "Revision"    INTEGER NOT NULL DEFAULT 0,   -- +1 with every change (0 = never edited)
    "CreatedAt"   TEXT NOT NULL,
    "UpdatedAt"   TEXT NOT NULL,
    CONSTRAINT "UQ_{LIST_TABLE}_Owner_Kind_Name" UNIQUE ("Owner", "Kind", "Name"),
    CONSTRAINT "CK_{LIST_TABLE}_Kind" CHECK ("Kind" IN ('watch', 'selected', 'screen'))
)
"""

ITEM_DDL = f"""
CREATE TABLE IF NOT EXISTS "{ITEM_TABLE}" (
    "ListId"      INTEGER NOT NULL REFERENCES "{LIST_TABLE}" ("ListId") ON DELETE CASCADE,
    "Position"    INTEGER NOT NULL,             -- the order of the list (gaps allowed)
    "Ticker"      TEXT NOT NULL,
    "AddedAt"     TEXT NOT NULL,
    CONSTRAINT "PK_{ITEM_TABLE}" PRIMARY KEY ("ListId", "Ticker")
)
"""

INDEXES = (
    f'CREATE INDEX IF NOT EXISTS "IX_{ITEM_TABLE}_Position" ON "{ITEM_TABLE}" ("ListId", "Position")',
)


def render_ddl() -> str:
    """The DDL as a script (``sql/create_chart_lists_sqlite.sql``)."""
    return "\n".join(s.strip() + ";" for s in (LIST_DDL, ITEM_DDL, *INDEXES)) + "\n"


class ListError(Exception):
    """A bad request on the lists (HTTP 400); :class:`ListNotFound` = 404."""


class ListNotFound(ListError):
    """No such list (for this owner)."""


class ListsUnavailable(Exception):
    """The list storage cannot be written / read (HTTP 503), e.g. a read-only database file."""


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def normalize_ticker(text: str | None) -> str | None:
    """``' pfe '`` -> ``'PFE'`` (quotes around the symbol are dropped); None when it is not a ticker."""
    t = (text or "").strip().strip("\"'").strip().upper()
    return t if TICKER_RE.match(t) else None


def parse_symbols(text: str | None) -> tuple[list[str], list[str]]:
    """The symbols of a screen-list file: separated by commas (line breaks, semicolons and blanks work
    too). Returns ``(tickers, invalid)`` - tickers upper case, unique, in file order."""
    tickers: list[str] = []
    invalid: list[str] = []
    for token in _SPLIT_RE.split((text or "").lstrip("\ufeff")):
        token = token.strip()
        if not token:
            continue
        t = normalize_ticker(token)
        if t is None:
            if token not in invalid:
                invalid.append(token[:40])
        elif t not in tickers:
            tickers.append(t)
    return tickers, invalid


def decode_file(data: bytes) -> str:
    """The text of an uploaded file: UTF-8 (with or without BOM), else Windows-1252."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def list_name_from_file(filename: str | None) -> str:
    """``tech picks.txt`` -> ``tech picks`` (letters, digits, blanks, ``. - _ ( )``; at most 60 characters)."""
    base = Path((filename or "").replace("\\", "/")).name
    stem = base.rsplit(".", 1)[0] if "." in base else base            # ".txt" -> "" (Path.stem keeps it)
    name = " ".join(_NAME_RE.sub(" ", stem).split())[:60]
    return name or "Screen list"


def clean_name(name: str | None) -> str:
    cleaned = " ".join(_NAME_RE.sub(" ", name or "").split())[:60]
    if not cleaned:
        raise ListError("a list name is required (letters, digits, blanks, . - _ ( ))")
    return cleaned


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
@dataclass
class ChartList:
    list_id: int
    kind: str
    name: str
    owner: str
    source_file: str | None
    revision: int
    created_at: str
    updated_at: str
    count: int = 0
    tickers: list[str] | None = field(default=None)

    @property
    def deletable(self) -> bool:
        return self.kind == KIND_SCREEN

    def as_dict(self) -> dict:
        out = {"id": self.list_id, "kind": self.kind, "name": self.name, "source_file": self.source_file,
               "count": self.count, "edited": self.revision > 0, "deletable": self.deletable,
               "created_at": self.created_at, "updated_at": self.updated_at}
        if self.tickers is not None:
            out["tickers"] = list(self.tickers)
        return out


_LIST_COLS = ("ListId", "Kind", "Name", "Owner", "SourceFile", "Revision", "CreatedAt", "UpdatedAt")


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class ListStore:
    """The ``Chart_List`` / ``Chart_List_Item`` tables (read-write)."""

    def __init__(self, path: str | Path, watch_seed: Iterable[str] | Callable[[], Iterable[str]] = ()):
        """*watch_seed*: the tickers a new watchlist starts with - or a function returning them
        (called when a watchlist is created or reset)."""
        self.path = Path(path)
        self._seed = watch_seed
        self._ready = False
        self._lock = threading.Lock()

    @property
    def watch_seed(self) -> list[str]:
        seed = self._seed() if callable(self._seed) else self._seed
        return list(dict.fromkeys(t for t in (normalize_ticker(x) for x in seed) if t))[:LIST_MAX]

    # -- storage ------------------------------------------------------------------------
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.parent.is_dir():
            raise ListsUnavailable(f"the folder of the list database {self.path} does not exist")
        try:
            conn = sqlite3.connect(str(self.path), timeout=15.0)
        except sqlite3.Error as exc:
            raise ListsUnavailable(f"cannot open the list database {self.path}: {exc}") from exc
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if not self._ready:
                with self._lock:
                    for ddl in (LIST_DDL, ITEM_DDL, *INDEXES):
                        conn.execute(ddl)
                    conn.commit()
                    self._ready = True
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            raise ListsUnavailable(f"the lists cannot be saved in {self.path.name}: {exc} - give the dashboard write "
                                   f"access to the file or set [lists] sqlite_path") from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _rows(conn, where: str, params: tuple) -> list[ChartList]:
        cols = ", ".join(f'l."{c}"' for c in _LIST_COLS)
        sql = (f'SELECT {cols}, (SELECT COUNT(*) FROM "{ITEM_TABLE}" i WHERE i."ListId" = l."ListId") '
               f'FROM "{LIST_TABLE}" l WHERE {where} '
               f'ORDER BY CASE l."Kind" WHEN \'watch\' THEN 0 WHEN \'selected\' THEN 1 ELSE 2 END, '
               f'l."Name" COLLATE NOCASE, l."ListId"')
        return [ChartList(*r[:8], count=int(r[8])) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _tickers(conn, list_id: int) -> list[str]:
        return [r[0] for r in conn.execute(f'SELECT "Ticker" FROM "{ITEM_TABLE}" WHERE "ListId" = ? '
                                           f'ORDER BY "Position", "AddedAt", "Ticker"', (list_id,))]

    @staticmethod
    def _touch(conn, list_id: int) -> None:
        conn.execute(f'UPDATE "{LIST_TABLE}" SET "Revision" = "Revision" + 1, "UpdatedAt" = ? WHERE "ListId" = ?',
                     (_now(), list_id))

    @staticmethod
    def _append(conn, list_id: int, tickers: Iterable[str]) -> list[str]:
        """Append the tickers not yet in the list; returns those added."""
        have = set(ListStore._tickers(conn, list_id))
        position = conn.execute(f'SELECT COALESCE(MAX("Position"), 0) FROM "{ITEM_TABLE}" WHERE "ListId" = ?',
                                (list_id,)).fetchone()[0]
        added, now = [], _now()
        for t in tickers:
            if t in have:
                continue
            if len(have) >= LIST_MAX:
                raise ListError(f"a list holds at most {LIST_MAX} stocks")
            position += 1
            conn.execute(f'INSERT INTO "{ITEM_TABLE}" ("ListId", "Position", "Ticker", "AddedAt") VALUES (?, ?, ?, ?)',
                         (list_id, position, t, now))
            have.add(t)
            added.append(t)
        return added

    def _create(self, conn, owner: str, kind: str, name: str, source_file: str | None = None,
                tickers: Iterable[str] = ()) -> int:
        now = _now()
        cur = conn.execute(f'INSERT INTO "{LIST_TABLE}" ("Kind", "Name", "Owner", "SourceFile", "Revision", '
                           f'"CreatedAt", "UpdatedAt") VALUES (?, ?, ?, ?, 0, ?, ?)',
                           (kind, name, owner, source_file, now, now))
        list_id = int(cur.lastrowid)
        self._append(conn, list_id, tickers)
        return list_id

    def ensure_defaults(self, owner: str = "") -> None:
        """The watchlist (seeded from [chart] watchlist) and the Selected list of *owner* exist."""
        with self.connection() as conn:
            for kind, name in DEFAULT_NAMES.items():
                exists = conn.execute(f'SELECT 1 FROM "{LIST_TABLE}" WHERE "Owner" = ? AND "Kind" = ?',
                                      (owner, kind)).fetchone()
                if not exists:
                    try:
                        self._create(conn, owner, kind, name, tickers=self.watch_seed if kind == KIND_WATCH else ())
                    except sqlite3.IntegrityError:          # created by a parallel request
                        pass

    # -- reading --------------------------------------------------------------------------------
    def lists(self, owner: str = "", with_items: tuple[str, ...] = (KIND_WATCH, KIND_SELECTED)) -> list[ChartList]:
        """Every list of *owner* (watchlist, Selected, screen lists by name); the tickers of the
        kinds in *with_items* are included."""
        self.ensure_defaults(owner)
        with self.connection() as conn:
            found = self._rows(conn, 'l."Owner" = ?', (owner,))
            for lst in found:
                if lst.kind in with_items:
                    lst.tickers = self._tickers(conn, lst.list_id)
        return found

    def get(self, list_id: int, owner: str = "") -> ChartList:
        with self.connection() as conn:
            found = self._rows(conn, 'l."ListId" = ? AND l."Owner" = ?', (int(list_id), owner))
            if not found:
                raise ListNotFound(f"list {list_id} does not exist")
            lst = found[0]
            lst.tickers = self._tickers(conn, lst.list_id)
        return lst

    def of_kind(self, kind: str, owner: str = "") -> ChartList:
        """The watchlist / the Selected list of *owner*."""
        if kind not in DEFAULT_NAMES:
            raise ListError(f"'{kind}' is not watch or selected")
        self.ensure_defaults(owner)
        with self.connection() as conn:
            list_id = conn.execute(f'SELECT "ListId" FROM "{LIST_TABLE}" WHERE "Owner" = ? AND "Kind" = ?',
                                   (owner, kind)).fetchone()[0]
        return self.get(list_id, owner)

    # -- changing -------------------------------------------------------------------------------
    def add(self, list_id: int, tickers: Iterable[str], owner: str = "") -> tuple[ChartList, list[str], list[str]]:
        """Append *tickers* (normalised) to the list: ``(list, added, already in the list)``."""
        wanted = list(dict.fromkeys(t for t in (normalize_ticker(x) for x in tickers) if t))
        self.get(list_id, owner)
        with self.connection() as conn:
            have = set(self._tickers(conn, int(list_id)))
            already = [t for t in wanted if t in have]
            added = self._append(conn, int(list_id), wanted)
            if added:
                self._touch(conn, int(list_id))
        return self.get(list_id, owner), added, already

    def remove(self, list_id: int, ticker: str, owner: str = "") -> tuple[ChartList, bool]:
        t = normalize_ticker(ticker)
        if t is None:
            raise ListError(f"'{ticker}' is not a valid ticker")
        self.get(list_id, owner)
        with self.connection() as conn:
            removed = conn.execute(f'DELETE FROM "{ITEM_TABLE}" WHERE "ListId" = ? AND "Ticker" = ?',
                                   (int(list_id), t)).rowcount > 0
            if removed:
                self._touch(conn, int(list_id))
        return self.get(list_id, owner), removed

    def replace(self, list_id: int, tickers: Iterable[str], owner: str = "", source_file: str | None = None) -> ChartList:
        """The list holds exactly *tickers* (in this order) afterwards."""
        wanted = list(dict.fromkeys(t for t in (normalize_ticker(x) for x in tickers) if t))
        if len(wanted) > LIST_MAX:
            raise ListError(f"a list holds at most {LIST_MAX} stocks (got {len(wanted)})")
        lst = self.get(list_id, owner)
        with self.connection() as conn:
            conn.execute(f'DELETE FROM "{ITEM_TABLE}" WHERE "ListId" = ?', (lst.list_id,))
            self._append(conn, lst.list_id, wanted)
            if source_file is not None:
                conn.execute(f'UPDATE "{LIST_TABLE}" SET "SourceFile" = ? WHERE "ListId" = ?', (source_file, lst.list_id))
            self._touch(conn, lst.list_id)
        return self.get(list_id, owner)

    def reset_watchlist(self, owner: str = "") -> ChartList:
        """The watchlist holds the default list ([chart] watchlist) again."""
        watch = self.of_kind(KIND_WATCH, owner)
        return self.replace(watch.list_id, self.watch_seed, owner)

    def save_screen(self, name: str, tickers: Iterable[str], source_file: str | None = None,
                    owner: str = "") -> tuple[ChartList, bool]:
        """Create the screen list *name* - or replace the stocks of the one with that name.
        Returns ``(list, replaced)``."""
        name = clean_name(name)
        wanted = list(dict.fromkeys(t for t in (normalize_ticker(x) for x in tickers) if t))
        if not wanted:
            raise ListError("the file contains no stock symbols (separate them with commas, e.g. HD, JNJ, PFE)")
        if len(wanted) > LIST_MAX:
            raise ListError(f"a list holds at most {LIST_MAX} stocks (the file has {len(wanted)})")
        with self.connection() as conn:
            row = conn.execute(f'SELECT "ListId" FROM "{LIST_TABLE}" WHERE "Owner" = ? AND "Kind" = ? AND "Name" = ?',
                               (owner, KIND_SCREEN, name)).fetchone()
            if row is None:
                screens = conn.execute(f'SELECT COUNT(*) FROM "{LIST_TABLE}" WHERE "Owner" = ? AND "Kind" = ?',
                                       (owner, KIND_SCREEN)).fetchone()[0]
                if screens >= SCREEN_LISTS_MAX:
                    raise ListError(f"at most {SCREEN_LISTS_MAX} screen lists - delete one first")
                list_id = self._create(conn, owner, KIND_SCREEN, name, source_file, wanted)
                replaced = False
        if row is not None:
            return self.replace(row[0], wanted, owner, source_file=source_file), True
        log.info("screen list '%s' created with %d stock(s)", name, len(wanted))
        return self.get(list_id, owner), replaced

    def delete(self, list_id: int, owner: str = "") -> ChartList:
        """Delete a screen list (the watchlist and the Selected list can only be emptied)."""
        lst = self.get(list_id, owner)
        if not lst.deletable:
            raise ListError(f"the {lst.name} list cannot be deleted - remove its stocks instead")
        with self.connection() as conn:
            conn.execute(f'DELETE FROM "{ITEM_TABLE}" WHERE "ListId" = ?', (lst.list_id,))
            conn.execute(f'DELETE FROM "{LIST_TABLE}" WHERE "ListId" = ?', (lst.list_id,))
        return lst
