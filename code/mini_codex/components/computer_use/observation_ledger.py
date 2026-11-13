"""Teaching protocol reducer only. No browser, device, Loop, or live verification."""
from dataclasses import dataclass
from urllib.parse import urlsplit


class Rejected(ValueError):
    pass


def exact_origin(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise Rejected("invalid_origin")
    try:
        port = p.port if p.port is not None else (443 if p.scheme == "https" else 80)
    except ValueError as e:
        raise Rejected("invalid_origin") from e
    return p.scheme, p.hostname.lower(), port


@dataclass(frozen=True)
class Snapshot:
    session: str
    epoch: int
    sequence: int
    revision: int
    origin: tuple
    selected_status: str
    # Rows are observed data, not an expected-answer fixture in the reducer.
    rows: tuple[tuple[str, str], ...]


@dataclass
class Attempt:
    action_id: str
    before: Snapshot
    phase: str = "dispatched"
    receipt: str | None = None
    verified: str | None = None
    cancelled: bool = False


class ObservationLedger:
    """Inputs must be trusted adapter observations, never model-declared state.

    Revision comparison detects reported drift. It cannot make a remote click
    atomic with the check or detect provider changes that were never reported.
    """
    def __init__(self, session, allowed_origin):
        self.session = session
        self.origin = exact_origin(allowed_origin)
        self.epoch = 1
        self.sequence = 0
        self.latest = None
        self.closed = False
        self.attempts = {}

    def observe(self, *, url, revision, selected_status, rows):
        if self.closed:
            raise Rejected("session_closed")
        if exact_origin(url) != self.origin:
            # An out-of-scope navigation invalidates earlier in-scope snapshots.
            self.latest = None
            self.epoch += 1
            raise Rejected("origin_denied")
        self.sequence += 1
        self.latest = Snapshot(self.session, self.epoch, self.sequence, revision,
                               self.origin, selected_status, tuple(rows))
        return self.latest

    def begin_dispatch(self, action_id, before, *, current_url, current_revision,
                       granted, cancelled=False):
        if self.closed:
            raise Rejected("session_closed")
        if cancelled:
            raise Rejected("cancelled_before_dispatch")
        if not granted:
            raise Rejected("permission_denied")
        if action_id in self.attempts:
            raise Rejected("duplicate_action")
        if (before != self.latest or before.session != self.session or
                before.epoch != self.epoch or exact_origin(current_url) != self.origin or
                current_revision != before.revision):
            raise Rejected("stale_or_wrong_target")
        self.attempts[action_id] = Attempt(action_id, before)
        # The caller must persist intent before actually sending an input.
        # Merely reaching here has not sent a click.
        return self.attempts[action_id]

    def receipt(self, action_id, value):
        a = self.attempts[action_id]
        if a.cancelled or a.phase != "dispatched":
            return False  # Late receipt never revives a cancelled activity.
        if value not in {"acknowledged", "transport_lost"}:
            raise Rejected("invalid_receipt")
        a.receipt = value
        a.phase = "awaiting_verification"
        return True

    def cancel(self, action_id):
        a = self.attempts[action_id]
        a.cancelled = True
        a.phase = "cancelled_outcome_unknown"
        # Cancelling local control is not evidence of undoing an external action.

    def verify_status_filter(self, action_id, after, *, status, expected_ids):
        a = self.attempts[action_id]
        if (self.closed or after != self.latest or after.session != self.session or
                after.epoch != self.epoch or after.sequence <= a.before.sequence):
            raise Rejected("fresh_observation_required")
        ids = [row[0] for row in after.rows]
        # Exact identity/count checks avoid all([]) vacuous success and duplicates.
        passed = (after.selected_status == status and
                  len(ids) == len(set(ids)) and set(ids) == set(expected_ids) and
                  all(row[1] == status for row in after.rows))
        a.verified = "goal_observed" if passed else "goal_not_observed"
        # Keep cancelled as a control-plane fact, separate from goal observation.
        if not a.cancelled:
            a.phase = "verified" if passed else "verification_failed"
        return a.verified

    def reopen(self):
        self.epoch += 1
        self.latest = None
        self.closed = False
        # Journalled intents can survive, but old snapshots cannot.

    def close(self):
        self.closed = True
        self.latest = None
