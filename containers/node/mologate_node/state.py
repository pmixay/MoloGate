"""What the node remembers about its containers, in a small SQLite file next to their sockets.

Docker knows each container's state; this file knows which chat it belongs to, its Open Terminal
key, whether its network is on and when it was last used. Writes are rare and tiny.
"""

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class Sandbox:
    context: str
    container_id: str
    name: str
    volume: str
    socket_dir: str
    api_key: str
    network: bool = True
    spare: bool = False
    created_at: float = 0.0
    last_active: float = 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data.pop('api_key')
        return data


class StateStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sandbox (
                context TEXT PRIMARY KEY,
                container_id TEXT NOT NULL,
                name TEXT NOT NULL,
                volume TEXT NOT NULL,
                socket_dir TEXT NOT NULL,
                api_key TEXT NOT NULL,
                network INTEGER NOT NULL DEFAULT 1,
                spare INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                last_active REAL NOT NULL
            )
            """
        )

    def _row(self, row) -> Sandbox | None:
        if row is None:
            return None
        return Sandbox(
            context=row[0],
            container_id=row[1],
            name=row[2],
            volume=row[3],
            socket_dir=row[4],
            api_key=row[5],
            network=bool(row[6]),
            spare=bool(row[7]),
            created_at=row[8],
            last_active=row[9],
        )

    _COLUMNS = 'context, container_id, name, volume, socket_dir, api_key, network, spare, created_at, last_active'

    def get(self, context: str) -> Sandbox | None:
        with self._lock:
            return self._row(
                self._db.execute(f'SELECT {self._COLUMNS} FROM sandbox WHERE context = ?', (context,)).fetchone()
            )

    def all(self) -> list[Sandbox]:
        with self._lock:
            return [self._row(row) for row in self._db.execute(f'SELECT {self._COLUMNS} FROM sandbox').fetchall()]

    def put(self, sandbox: Sandbox) -> None:
        now = time.time()
        sandbox.created_at = sandbox.created_at or now
        sandbox.last_active = sandbox.last_active or now
        with self._lock:
            self._db.execute(
                f'INSERT OR REPLACE INTO sandbox ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    sandbox.context,
                    sandbox.container_id,
                    sandbox.name,
                    sandbox.volume,
                    sandbox.socket_dir,
                    sandbox.api_key,
                    int(sandbox.network),
                    int(sandbox.spare),
                    sandbox.created_at,
                    sandbox.last_active,
                ),
            )

    def rename(self, old_context: str, new_context: str) -> None:
        with self._lock:
            self._db.execute(
                'UPDATE sandbox SET context = ?, spare = 0, last_active = ? WHERE context = ?',
                (new_context, time.time(), old_context),
            )

    def set_network(self, context: str, enabled: bool) -> None:
        with self._lock:
            self._db.execute('UPDATE sandbox SET network = ? WHERE context = ?', (int(enabled), context))

    def touch_many(self, activity: dict[str, float]) -> None:
        if not activity:
            return
        with self._lock:
            self._db.executemany(
                'UPDATE sandbox SET last_active = MAX(last_active, ?) WHERE context = ?',
                [(ts, context) for context, ts in activity.items()],
            )

    def delete(self, context: str) -> None:
        with self._lock:
            self._db.execute('DELETE FROM sandbox WHERE context = ?', (context,))
