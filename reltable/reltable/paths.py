"""Single place for filesystem locations. Source datasets live next to this package."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # .../Stride_dataset/reltable
DATASETS = ROOT.parent                              # .../Stride_dataset
BIRD = DATASETS / "bird"
BIRD_TRAIN = BIRD / "train-2"                        # BIRD train release (69 databases); db ids are disjoint from BIRD dev
SPIDER = DATASETS / "spider"
DATA = ROOT / "data"
DATA_V1 = DATA / "v1"                                # DREDGE v1 (train/dev/test splits); the pilot files in DATA stay untouched
REPORTS = ROOT / "reports"
MODELS = ROOT / "models"
EMBED_MODEL = MODELS / "bge-small-en-v1.5"           # downloaded manually (no network access from the pipeline)


def sqlite_path(source: str, db_id: str) -> Path:
    if source == "bird":
        p = BIRD / "dev_databases" / db_id / f"{db_id}.sqlite"
        return p if p.exists() else BIRD_TRAIN / "train_databases" / db_id / f"{db_id}.sqlite"
    if source == "spider":
        return SPIDER / "database" / db_id / f"{db_id}.sqlite"
    raise ValueError(source)


def origin(source: str, db_id: str) -> str:
    """Which release a database comes from: bird_dev, bird_train, spider_dev or spider_train."""
    if source == "bird":
        return "bird_dev" if (BIRD / "dev_databases" / db_id).exists() else "bird_train"
    return "spider_dev" if db_id in _spider_dev_dbs() else "spider_train"


_SPIDER_DEV = None


def _spider_dev_dbs():
    global _SPIDER_DEV
    if _SPIDER_DEV is None:
        import json
        _SPIDER_DEV = {q["db_id"] for q in json.load(open(SPIDER / "dev.json"))}
    return _SPIDER_DEV


def active():
    """Files of the dataset the harness and baselines work on, chosen by environment variables so every existing
    command keeps its meaning: DREDGE_SPLIT = pilot (default) | test | dev | train, DREDGE_TIER = random (default) | hard.
    The tier only changes which decoy manifests are used; questions and evidence are the same."""
    import os
    split = os.environ.get("DREDGE_SPLIT", "pilot")
    tier = os.environ.get("DREDGE_TIER", "random")
    if split == "pilot":
        if tier != "random":
            raise ValueError("the pilot has only the random decoy tier")
        return {"split": split, "tier": tier, "evidence_draft": DATA / "evidence_draft.jsonl",
                "evidence_rows": DATA / "evidence_rows.jsonl", "manifests": DATA / "decoy_manifests.jsonl",
                "pool": DATA / "decoy_pool.json", "cache": DATA / "cache"}
    if split not in ("test", "dev", "train") or tier not in ("random", "hard"):
        raise ValueError(f"DREDGE_SPLIT={split} DREDGE_TIER={tier}")
    return {"split": split, "tier": tier, "evidence_draft": DATA_V1 / f"{split}_evidence_draft.jsonl",
            "evidence_rows": DATA_V1 / f"{split}_evidence_rows.jsonl",
            "manifests": DATA_V1 / f"{split}_decoys_{tier}.jsonl",
            "pool": DATA_V1 / "decoy_pool.json", "cache": DATA_V1 / "cache"}
