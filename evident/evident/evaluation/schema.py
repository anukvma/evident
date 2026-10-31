"""Prediction format. One JSON object per line; one line per (system, qid, level).

{
  "system": "my-system",              # free text, used to group results
  "qid": "bird-123",                  # from data/pilot.jsonl
  "level": 0,                         # decoy tables added: 0 | 10 | 50 | 100
  "sql": "SELECT ...",                # optional. If present the HARNESS executes it on the variant
  "answer": [[...], ...],             # optional result rows; used only when "sql" is absent
  "answer_values": ["a", 3, ...],     # optional FLAT list of values, for systems that answer without SQL
  "ranked_tables": ["t1", "t2", ...], # optional, best first (for Recall@k)
  "selected_tables": ["t1", ...],     # optional, the tables finally put in the model's context
  "selected_columns": {"t1": ["a", "b"], "t2": ["*"]},   # optional; "*" = all columns
  "join_keys": [["t1.a", "t2.b"], ...],                  # optional, unordered pairs
  "retrieved_rows": {"t1": [1, 5, 9], "t2": "ALL"},      # optional rowids; "ALL" = whole table
  "abstained": false,
  "evidence_source": "sql_derived",    # optional; set when columns/joins/rows were derived from the SQL itself
  "cost": {"latency_s": 1.2, "preprocess_s": 30.0, "llm_calls": 3,
           "input_tokens": 4100, "output_tokens": 220, "peak_mem_mb": 800}
}
Every field except system/qid/level is optional. A metric whose inputs are missing is reported as
unavailable (NaN), never as zero. A missing (qid, level) line counts as a wrong, empty answer.
"""
from dataclasses import dataclass, field

LEVELS = (0, 10, 50, 100, 250, 500)      # all valid levels
DEFAULT_LEVELS = (0, 10, 50, 100)           # what tools run unless told otherwise (250/500 are expensive)


@dataclass
class Prediction:
    system: str
    qid: str
    level: int
    sql: str | None = None
    answer: list | None = None
    answer_values: list | None = None   # flat list of answer values (for systems that answer in text)
    ranked_tables: list | None = None
    selected_tables: list | None = None
    selected_columns: dict | None = None
    join_keys: list | None = None
    retrieved_rows: dict | None = None
    abstained: bool = False
    evidence_source: str | None = None   # 'sql_derived' = columns/joins/rows come from the system's own SQL
    cost: dict = field(default_factory=dict)
    llm_error: str | None = None        # set by the runner when the model call failed (transport error, 401, timeout)
    missing: bool = False

    @classmethod
    def from_json(cls, d: dict) -> "Prediction":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        p = cls(**known)
        if p.level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}, got {p.level}")
        return p

    @classmethod
    def empty(cls, system, qid, level) -> "Prediction":
        return cls(system=system, qid=qid, level=level, missing=True)
