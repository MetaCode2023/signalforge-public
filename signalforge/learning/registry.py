"""Experiment registry: the memory of everything that was tried.

An experiment that cannot be reproduced is not evidence, so every row carries
the code version, parameters, dataset fingerprint, windows, cost assumptions
and seed. The id is a hash of the content that defines the experiment, so the
same experiment cannot sit in the registry twice under different names.

Hold-out discipline is enforced here, not requested politely: the test window
may be evaluated once per (strategy, parameters, dataset fingerprint). A second
evaluation raises HoldoutViolation and is logged as a flagged violation in
`test_evaluations`, so the attempt itself stays on the record.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from signalforge.core.errors import PromotionError, ValidationError

GIT_UNAVAILABLE = "git-unavailable"
WINDOWS = ("train", "validation", "test")
DECISIONS = ("UNDECIDED", "PROPOSED", "APPROVED", "REJECTED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_failures (
    attempt_id VARCHAR PRIMARY KEY,
    strategy_name VARCHAR NOT NULL,
    strategy_version VARCHAR,
    parameters VARCHAR NOT NULL,
    evaluated_window VARCHAR NOT NULL,
    dataset_fingerprint VARCHAR,
    random_seed BIGINT,
    exception_type VARCHAR NOT NULL,
    occurred_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id VARCHAR PRIMARY KEY,
    strategy_name VARCHAR NOT NULL,
    strategy_version VARCHAR,
    git_commit VARCHAR,
    parameters VARCHAR NOT NULL,
    dataset_fingerprint VARCHAR NOT NULL,
    dataset_description VARCHAR,
    evaluated_window VARCHAR NOT NULL,
    train_start DATE, train_end DATE,
    validation_start DATE, validation_end DATE,
    test_start DATE, test_end DATE,
    transaction_cost_assumptions VARCHAR,
    engine_version VARCHAR,
    random_seed BIGINT,
    created_at TIMESTAMP,
    hypothesis VARCHAR,
    metrics VARCHAR,
    max_drawdown DOUBLE,
    trade_count INTEGER,
    evidence_level VARCHAR,
    promotion_decision VARCHAR,
    promotion_reason VARCHAR,
    rejection_reason VARCHAR,
    decided_by VARCHAR,
    decided_at TIMESTAMP
);
CREATE SEQUENCE IF NOT EXISTS test_evaluation_seq;
CREATE TABLE IF NOT EXISTS test_evaluations (
    seq BIGINT DEFAULT nextval('test_evaluation_seq'),
    holdout_key VARCHAR NOT NULL,
    experiment_id VARCHAR NOT NULL,
    strategy_name VARCHAR NOT NULL,
    dataset_fingerprint VARCHAR NOT NULL,
    evaluated_at TIMESTAMP,
    violation BOOLEAN NOT NULL
);
"""

_COLUMNS = (
    "experiment_id", "strategy_name", "strategy_version", "git_commit", "parameters",
    "dataset_fingerprint", "dataset_description", "evaluated_window",
    "train_start", "train_end", "validation_start", "validation_end",
    "test_start", "test_end", "transaction_cost_assumptions", "engine_version",
    "random_seed", "created_at", "hypothesis", "metrics", "max_drawdown",
    "trade_count", "evidence_level", "promotion_decision", "promotion_reason",
    "rejection_reason", "decided_by", "decided_at",
)
_JSON_COLUMNS = {"parameters", "transaction_cost_assumptions", "metrics"}


class HoldoutViolation(ValidationError):
    """The test window was evaluated a second time for the same strategy, parameters and data."""


@dataclass(frozen=True)
class StrategySpec:
    """A strategy named by id and parameters, never by instance.

    The learning layer holds specs, not strategy objects, so nothing in this
    package can hand a strategy a handle on its own promotion.
    """

    strategy_id: str
    params: dict = field(default_factory=dict)
    version: str = "1"

    def with_params(self, **overrides) -> "StrategySpec":
        return StrategySpec(self.strategy_id, {**self.params, **overrides}, self.version)

    def build(self):
        from signalforge.strategies.registry import build

        return build(self.strategy_id, **self.params)


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    strategy_name: str
    strategy_version: str
    git_commit: str
    parameters: dict
    dataset_fingerprint: str
    dataset_description: str
    evaluated_window: str
    train_start: date | None
    train_end: date | None
    validation_start: date | None
    validation_end: date | None
    test_start: date | None
    test_end: date | None
    transaction_cost_assumptions: dict
    engine_version: str
    random_seed: int
    created_at: datetime
    hypothesis: str
    metrics: dict
    max_drawdown: float | None
    trade_count: int | None
    evidence_level: str
    promotion_decision: str
    promotion_reason: str | None
    rejection_reason: str | None
    decided_by: str | None
    decided_at: datetime | None


def current_git_commit(cwd: str | Path | None = None) -> str:
    """HEAD sha, or GIT_UNAVAILABLE. Recording provenance must never crash a run."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(cwd or Path(__file__).resolve().parent),
        )
    except Exception:
        return GIT_UNAVAILABLE
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not sha:
        return GIT_UNAVAILABLE
    return sha


def _number(value) -> str:
    """One canonical text form for any numerically equal value.

    `str(Decimal)` preserves trailing zeros, so Decimal("0.05"),
    Decimal("0.050") and the float 0.05 produced three different hashes for
    one logical weight. Since the hash drives the hold-out guard, that was
    three free looks at the test window. Normalising collapses them.

    `normalize()` turns small values into exponent form (0.05 -> 5E-2), so
    the result is re-formatted with 'f' to keep a plain decimal string, and
    an integral value's trailing '.' is trimmed.
    """
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(dec.normalize(), "f")
    return text.rstrip(".") if text.endswith(".") else text


def _plain(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (Decimal, float)):
        return _number(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        # Unordered input must hash in a fixed order. Iteration order of a
        # set of strings varies between processes because str hashing is
        # seed-randomised, which made the hold-out key non-deterministic and
        # stopped the guard firing at all.
        return sorted((_plain(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def canonical_json(value) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _split_bounds(splits) -> dict[str, date | None]:
    names = ("train_start", "train_end", "validation_start", "validation_end", "test_start", "test_end")
    return {n: (getattr(splits, n) if splits is not None else None) for n in names}


class ExperimentRegistry:
    def __init__(self, warehouse) -> None:
        self._warehouse = warehouse
        self._con = warehouse.con
        self._con.execute(SCHEMA)

    def record_failure(self, *, strategy_spec, window, fingerprint, seed, exception_type):
        """Retain failed attempts without storing arbitrary exception messages."""
        self._con.execute(
            "INSERT INTO experiment_failures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [uuid.uuid4().hex, strategy_spec.strategy_id, strategy_spec.version,
             canonical_json(strategy_spec.params), window, fingerprint, seed,
             exception_type, datetime.now()],
        )

    # ------------------------------------------------------------------ ids
    @staticmethod
    def experiment_id_for(
        *, strategy_name, strategy_version, parameters, dataset_fingerprint,
        window, splits, transaction_cost_assumptions,
    ) -> str:
        content = {
            "strategy": strategy_name,
            "version": strategy_version,
            "parameters": parameters,
            "dataset": dataset_fingerprint,
            "window": window,
            "splits": _split_bounds(splits),
            "costs": transaction_cost_assumptions,
        }
        return "exp_" + _hash(content)[:20]

    @staticmethod
    def holdout_key(strategy_name, parameters, dataset_fingerprint) -> str:
        return _hash({"strategy": strategy_name, "parameters": parameters, "dataset": dataset_fingerprint})

    # ---------------------------------------------------------- hold-out guard
    def holdout_spent(self, *, strategy_name, parameters, dataset_fingerprint) -> bool:
        """Has this combination already spent its one look at the test window?

        A read-only probe. It deliberately does NOT log a violation: callers
        use it to decide whether to run the test window at all, and a probe
        that logged every time the CLI started would fill the audit trail
        with false positives and bury the real signal.
        """
        key = self.holdout_key(strategy_name, parameters, dataset_fingerprint)
        return bool(
            self._con.execute(
                "SELECT count(*) FROM test_evaluations WHERE holdout_key = ? "
                "AND NOT violation",
                [key],
            ).fetchone()[0]
        )

    def log_holdout_attempt(
        self, *, strategy_name, parameters, dataset_fingerprint, experiment_id="blocked"
    ) -> None:
        """Record that a second look was ATTEMPTED and refused, without raising.

        The probe above is read-only so it can be called freely, but a
        blocked attempt still belongs in the audit trail: "someone asked to
        re-open the hold-out" is exactly the kind of thing that should be
        visible later, even though nothing was re-run.
        """
        key = self.holdout_key(strategy_name, parameters, dataset_fingerprint)
        self._log_test_evaluation(key, experiment_id, strategy_name, dataset_fingerprint, True)

    def guard_test_window(self, *, strategy_name, parameters, dataset_fingerprint, experiment_id) -> None:
        """Raise HoldoutViolation (after logging it) if this test look was already spent."""
        key = self.holdout_key(strategy_name, parameters, dataset_fingerprint)
        spent = self._con.execute(
            "SELECT count(*) FROM test_evaluations WHERE holdout_key = ? AND NOT violation", [key]
        ).fetchone()[0]
        if spent:
            self._log_test_evaluation(key, experiment_id, strategy_name, dataset_fingerprint, True)
            raise HoldoutViolation(
                f"the test window was already evaluated for {strategy_name} with these "
                f"parameters on this dataset ({spent} prior evaluation(s)). A held-out "
                "period is only held out once; a second look is now a tuning set. "
                "The attempt has been logged as a violation."
            )

    def _log_test_evaluation(self, key, experiment_id, strategy_name, fingerprint, violation) -> None:
        self._con.execute(
            "INSERT INTO test_evaluations (holdout_key, experiment_id, strategy_name, "
            "dataset_fingerprint, evaluated_at, violation) VALUES (?, ?, ?, ?, ?, ?)",
            [key, experiment_id, strategy_name, fingerprint, datetime.now(), violation],
        )

    def test_evaluations_for(self, experiment_id: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT holdout_key, experiment_id, strategy_name, dataset_fingerprint, "
            "evaluated_at, violation FROM test_evaluations WHERE holdout_key IN "
            "(SELECT holdout_key FROM test_evaluations WHERE experiment_id = ?) ORDER BY seq",
            [experiment_id],
        ).fetchall()
        names = ("holdout_key", "experiment_id", "strategy_name", "dataset_fingerprint",
                 "evaluated_at", "violation")
        return [dict(zip(names, r)) for r in rows]

    # --------------------------------------------------------------- record
    def record(
        self, *, strategy_name, strategy_version, parameters, dataset_fingerprint,
        dataset_description, window, splits, transaction_cost_assumptions, engine_version,
        random_seed, hypothesis, metrics, max_drawdown, trade_count, evidence_level,
        git_commit: str | None = None,
    ) -> str:
        if window not in WINDOWS:
            raise ValidationError(f"window must be one of {WINDOWS}, got {window!r}")
        experiment_id = self.experiment_id_for(
            strategy_name=strategy_name, strategy_version=strategy_version,
            parameters=parameters, dataset_fingerprint=dataset_fingerprint, window=window,
            splits=splits, transaction_cost_assumptions=transaction_cost_assumptions,
        )
        if window == "test":
            self.guard_test_window(
                strategy_name=strategy_name, parameters=parameters,
                dataset_fingerprint=dataset_fingerprint, experiment_id=experiment_id,
            )
        elif self._exists(experiment_id):
            return experiment_id

        bounds = _split_bounds(splits)
        level = evidence_level.value if isinstance(evidence_level, Enum) else evidence_level
        row = [
            experiment_id, strategy_name, strategy_version,
            git_commit or current_git_commit(), canonical_json(parameters),
            dataset_fingerprint, dataset_description, window,
            bounds["train_start"], bounds["train_end"],
            bounds["validation_start"], bounds["validation_end"],
            bounds["test_start"], bounds["test_end"],
            canonical_json(transaction_cost_assumptions), engine_version, random_seed,
            datetime.now(), hypothesis, canonical_json(metrics),
            None if max_drawdown is None else float(max_drawdown), trade_count, level,
            "UNDECIDED", None, None, None, None,
        ]

        def write() -> None:
            self._con.execute(
                f"INSERT INTO experiments ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_COLUMNS))})",
                row,
            )
            if window == "test":
                self._log_test_evaluation(
                    self.holdout_key(strategy_name, parameters, dataset_fingerprint),
                    experiment_id, strategy_name, dataset_fingerprint, False,
                )

        # Atomic, because these are two writes that must agree. A failure
        # between them left the experiment recorded while the hold-out read as
        # unspent, so the same strategy on the same data could be evaluated
        # against the held-out window a second time and the registry would
        # permit it -- the one thing it exists to prevent.
        #
        # The guard above deliberately runs OUTSIDE this transaction. It logs
        # a refused second look as a violation and then raises, and a rollback
        # on the way out would erase that audit record.
        self._warehouse.transaction(write)
        return experiment_id

    def _exists(self, experiment_id: str) -> bool:
        return bool(self._con.execute(
            "SELECT count(*) FROM experiments WHERE experiment_id = ?", [experiment_id]
        ).fetchone()[0])

    # ----------------------------------------------------------------- read
    def _row_to_record(self, row) -> ExperimentRecord:
        values = {}
        for name, value in zip(_COLUMNS, row):
            values[name] = json.loads(value) if name in _JSON_COLUMNS and value is not None else value
        return ExperimentRecord(**values)

    def get(self, experiment_id: str) -> ExperimentRecord:
        row = self._con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM experiments WHERE experiment_id = ?", [experiment_id]
        ).fetchone()
        if row is None:
            raise ValidationError(f"no experiment {experiment_id!r} in the registry")
        return self._row_to_record(row)

    def list_experiments(self, strategy_name: str | None = None, decision: str | None = None) -> list[ExperimentRecord]:
        clauses, args = [], []
        if strategy_name is not None:
            clauses.append("strategy_name = ?")
            args.append(strategy_name)
        if decision is not None:
            clauses.append("promotion_decision = ?")
            args.append(decision)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM experiments{where} ORDER BY created_at, experiment_id", args
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self, strategy_name: str | None = None) -> int:
        if strategy_name is None:
            return self._con.execute("SELECT count(*) FROM experiments").fetchone()[0]
        return self._con.execute(
            "SELECT count(*) FROM experiments WHERE strategy_name = ?", [strategy_name]
        ).fetchone()[0]

    # ------------------------------------------------------------ decisions
    def record_decision(self, experiment_id: str, decision: str, reason: str, decided_by: str | None) -> None:
        if decision not in DECISIONS or decision == "UNDECIDED":
            raise ValidationError(f"decision must be one of {DECISIONS[1:]}, got {decision!r}")
        if not reason:
            raise ValidationError("a promotion decision must carry a reason")
        self.get(experiment_id)
        if decision == "APPROVED" and not (isinstance(decided_by, str) and decided_by.strip()):
            raise PromotionError("an approval must name the human who made it")
        self._con.execute(
            "UPDATE experiments SET promotion_decision = ?, promotion_reason = ?, "
            "rejection_reason = ?, decided_by = ?, decided_at = ? WHERE experiment_id = ?",
            [decision, reason, reason if decision == "REJECTED" else None,
             decided_by, datetime.now(), experiment_id],
        )
