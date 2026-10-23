from pathlib import Path
from app.db import engine


def init_db():
    with engine.begin() as conn:
        conn.exec_driver_sql(Path(__file__).resolve().parents[1].joinpath("schema.sql").read_text(encoding="utf-8"))


if __name__ == "__main__":
    init_db()
