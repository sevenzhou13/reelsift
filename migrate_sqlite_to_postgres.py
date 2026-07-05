from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, select

from db import metadata


TABLE_ORDER = [
    "libraries",
    "project_nodes",
    "clips",
    "clip_tags",
    "transcripts",
    "storyboards",
    "storyboard_items",
    "clip_node_refs",
    "recycled_clips",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate Reelsift data from SQLite to PostgreSQL.")
    parser.add_argument(
        "--source",
        default="sqlite:///./data/reelsift.db",
        help="Source database URL. Defaults to the local SQLite file.",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Target PostgreSQL database URL, e.g. postgresql+psycopg://user:pass@host:5432/reelsift",
    )
    return parser.parse_args()


def reflect_source_tables(source_engine) -> dict[str, Table]:
    source_metadata = MetaData()
    source_metadata.reflect(bind=source_engine)
    return {table_name: source_metadata.tables[table_name] for table_name in TABLE_ORDER}


def migrate_table(table_name: str, source_tables: dict[str, Table], source_engine, target_engine) -> int:
    source_table = source_tables[table_name]
    target_table = metadata.tables[table_name]

    with source_engine.connect() as source_conn:
        rows = source_conn.execute(select(source_table)).mappings().all()

    if not rows:
        return 0

    payload = [dict(row) for row in rows]
    with target_engine.begin() as target_conn:
        target_conn.execute(target_table.delete())
        target_conn.execute(target_table.insert(), payload)
    return len(payload)


def main() -> None:
    args = parse_args()
    source_engine = create_engine(args.source, future=True)
    target_engine = create_engine(args.target, future=True)

    metadata.create_all(target_engine)
    source_tables = reflect_source_tables(source_engine)

    for table_name in TABLE_ORDER:
        copied = migrate_table(table_name, source_tables, source_engine, target_engine)
        print(f"{table_name}: copied {copied} rows")


if __name__ == "__main__":
    main()
