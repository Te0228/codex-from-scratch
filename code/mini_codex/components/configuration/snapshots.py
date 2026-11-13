"""Offline teaching prototype: immutable request capture and guarded catalog install.

This module does not read credentials, configuration files, environment variables,
or the network. It is not a Loop, an auth implementation, or a chapter checkpoint.
"""
from dataclasses import dataclass, replace
from threading import RLock


class RouteError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigSnapshot:
    version: str
    provider: str
    model: str


@dataclass(frozen=True)
class AuthLease:
    actor: str
    source_generation: int
    credential_revision: int


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    supports: frozenset[str]


@dataclass(frozen=True)
class CatalogSnapshot:
    version: str
    config_version: str
    provider: str
    actor: str
    source_generation: int
    models: tuple[ModelCapabilities, ...]


@dataclass(frozen=True)
class RefreshTicket:
    ticket_id: int
    config_version: str
    provider: str
    actor: str
    source_generation: int


@dataclass(frozen=True)
class RequestSnapshot:
    request_id: str
    config: ConfigSnapshot
    lease: AuthLease
    catalog_version: str
    capabilities: ModelCapabilities


class SnapshotOwner:
    """One lock is the prototype's publication/capture boundary, never held over I/O."""

    def __init__(self, config: ConfigSnapshot, actor: str):
        self._lock = RLock()
        self._config = config
        self._lease = AuthLease(actor, 1, 1)
        self._catalog = None
        self._tickets: dict[int, RefreshTicket] = {}
        self._next_ticket = 0

    def update_config(self, candidate: ConfigSnapshot) -> None:
        with self._lock:
            self._config = candidate

    def switch_actor(self, actor: str) -> None:
        with self._lock:
            self._lease = AuthLease(actor, self._lease.source_generation + 1, 1)

    def renew_same_source(self, expected: AuthLease) -> AuthLease:
        """Models only the version rule; no token exchange or persistence occurs."""
        with self._lock:
            if (expected.actor, expected.source_generation) != (
                self._lease.actor, self._lease.source_generation
            ):
                raise RouteError("identity_changed")
            self._lease = replace(
                self._lease, credential_revision=self._lease.credential_revision + 1
            )
            return self._lease

    def begin_refresh(self) -> RefreshTicket:
        with self._lock:
            self._next_ticket += 1
            ticket = RefreshTicket(
                self._next_ticket, self._config.version, self._config.provider,
                self._lease.actor, self._lease.source_generation,
            )
            self._tickets[ticket.ticket_id] = ticket
            return ticket

    def cancel_refresh(self, ticket: RefreshTicket) -> None:
        with self._lock:
            self._tickets.pop(ticket.ticket_id, None)

    def install(self, ticket: RefreshTicket, catalog: CatalogSnapshot) -> bool:
        with self._lock:
            registered = self._tickets.pop(ticket.ticket_id, None)
            if registered != ticket:
                return False
            expected = (
                self._config.version, self._config.provider,
                self._lease.actor, self._lease.source_generation,
            )
            submitted = (
                ticket.config_version, ticket.provider, ticket.actor,
                ticket.source_generation,
            )
            response = (
                catalog.config_version, catalog.provider, catalog.actor,
                catalog.source_generation,
            )
            if expected != submitted or submitted != response:
                return False
            names = [model.model for model in catalog.models]
            if len(names) != len(set(names)):
                raise RouteError("duplicate_model")
            self._catalog = catalog
            return True

    def capture(self, request_id: str, required: frozenset[str]) -> RequestSnapshot:
        with self._lock:
            c, a, catalog = self._config, self._lease, self._catalog
            if catalog is None or (
                catalog.config_version, catalog.provider,
                catalog.actor, catalog.source_generation,
            ) != (c.version, c.provider, a.actor, a.source_generation):
                raise RouteError("catalog_identity_or_config_mismatch")
            model = next((m for m in catalog.models if m.model == c.model), None)
            if model is None:
                raise RouteError("unknown_model")
            if not required <= model.supports:
                raise RouteError("unsupported_capability")
            return RequestSnapshot(request_id, c, a, catalog.version, model)
