import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://brains:brains_dev@localhost:5432/brains"
)


def query(sql: str, params: tuple = ()) -> list[dict]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def execute(sql: str, params: tuple = ()) -> list[dict] | int:
    """Run an INSERT/UPDATE/DELETE with bound params and commit.

    Returns the fetched rows when the statement has a RETURNING clause
    (``cur.description`` is populated); otherwise returns the affected
    ``rowcount``. Same parameterized style as ``query`` — never interpolate
    values into the SQL string.
    """
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else None
            conn.commit()
            return rows if rows is not None else cur.rowcount
