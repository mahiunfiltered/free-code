"""In-process write-scope leases: two holders never own overlapping globs at once."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .graph import scopes_overlap


@dataclass(frozen=True)
class Lease:
    owner: str
    node_id: str
    globs: tuple[str, ...]
    expires_at: float


class LeaseManager:
    # ponytail: in-process only (one server); persist in the Store if runs span processes.

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: list[Lease] = []

    def try_acquire(
        self, owner: str, node_id: str, globs: list[str], ttl_s: float
    ) -> Lease | None:
        """Grant the lease, or None when another holder's live lease overlaps."""

        with self._lock:
            now = self._clock()
            live = [held for held in self._leases if held.expires_at > now]
            others = [h for h in live if (h.owner, h.node_id) != (owner, node_id)]
            if any(scopes_overlap(list(h.globs), globs) for h in others):
                self._leases = live
                return None
            # Re-acquiring replaces the holder's previous lease.
            lease = Lease(owner, node_id, tuple(globs), now + ttl_s)
            self._leases = [*others, lease]
            return lease

    def release(self, lease: Lease) -> None:
        with self._lock:
            if lease in self._leases:
                self._leases.remove(lease)

    def active(self) -> list[Lease]:
        with self._lock:
            now = self._clock()
            return [lease for lease in self._leases if lease.expires_at > now]
