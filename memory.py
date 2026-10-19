"""Long term memory: past NPT incidents (cause -> fix), failed cases saved by the Critic
and engineer decisions. One SQLite file, read by Research, written by Critic and Engineer.

data/incidents.sqlite is the committed seed. The app reads and writes a working copy in runtime/,
so using the demo never changes the repository. Delete runtime/ (or press Reset in the Memory tab)
to start again from the seed."""
import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
SEED = ROOT / "data" / "incidents.sqlite"
DEFAULT_DB = Path(os.getenv("RIG_MEMORY_DB", ROOT / "runtime" / "memory.sqlite"))
DB = DEFAULT_DB


def _con():
    if not DB.exists():
        DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SEED, DB)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def set_db(path) -> None:
    global DB
    DB = Path(path)


def use_default() -> None:
    set_db(DEFAULT_DB)


def reset() -> None:
    """Throw away failed cases and engineer decisions; start again from the seed incidents."""
    DB.unlink(missing_ok=True)
    _con().close()


def search_incidents(well_id: str, categories: list[str], limit: int = 4) -> list[dict]:
    """Past incidents in the suspected categories, same well first, then the rest of the field."""
    if not categories:
        return []
    marks = ",".join("?" * len(categories))
    with _con() as con:
        rows = con.execute(
            f"""SELECT well_id, day, category, cause, fix, hours_lost FROM incidents
                WHERE category IN ({marks})
                ORDER BY (well_id = ?) DESC, hours_lost DESC LIMIT ?""",
            (*categories, well_id, limit)).fetchall()
    return [dict(r) | {"same_well": r["well_id"] == well_id} for r in rows]


def add_failed_case(report_id: str, attempt: int, problems: list[str]) -> None:
    with _con() as con:
        con.execute("INSERT INTO failed_cases (report_id, attempt, problems) VALUES (?,?,?)",
                    (report_id, attempt, " | ".join(problems)))


def add_feedback(report_id: str, decision: str, comment: str) -> None:
    with _con() as con:
        con.execute("INSERT INTO engineer_feedback (report_id, decision, comment) VALUES (?,?,?)",
                    (report_id, decision, comment))


def table(name: str) -> list[dict]:
    assert name in {"incidents", "failed_cases", "engineer_feedback"}
    with _con() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM {name} ORDER BY id DESC")]
