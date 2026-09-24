"""Commented-out code must never become an edge, and embedded SQL must.

These two requirements pull against each other: the scanner reaches into string
literals to find SQL, and a commented-out block is often just a string literal.
"""

from __future__ import annotations

import textwrap

import pytest


def joins_of(analyzer, text):
    found, _, _ = analyzer.analyze(textwrap.dedent(text))
    return found


class TestSqlComments:
    def test_line_comment_join_is_ignored(self, sql):
        found = joins_of(
            sql,
            """
            SELECT 1 FROM a.t1 x
            -- JOIN b.t2 y ON x.k = y.k
            JOIN c.t3 z ON x.k = z.k
            """,
        )
        pairs = {(j.left.key, j.right.key) for j in found}
        assert pairs == {("a.t1", "c.t3")}

    def test_block_comment_join_is_ignored(self, sql):
        found = joins_of(
            sql,
            """
            SELECT 1 FROM a.t1 x
            /* JOIN b.t2 y ON x.k = y.k */
            JOIN c.t3 z ON x.k = z.k
            """,
        )
        pairs = {(j.left.key, j.right.key) for j in found}
        assert pairs == {("a.t1", "c.t3")}

    def test_hash_comment_join_is_ignored(self, sql):
        """``#`` is a comment in MySQL and Hive, and people write it anyway."""
        found = joins_of(
            sql,
            """
            SELECT 1 FROM a.t1 x
            # JOIN b.t2 y ON x.k = y.k
            JOIN c.t3 z ON x.k = z.k
            """,
        )
        pairs = {(j.left.key, j.right.key) for j in found}
        assert pairs == {("a.t1", "c.t3")}

    def test_hash_comment_does_not_cost_the_statement(self, sql):
        """A rejected token used to lose the whole statement, valid joins too."""
        _, _, errors = sql.analyze("SELECT 1 FROM a.t1 x\n# note\nJOIN c.t3 z ON x.k = z.k")
        assert errors == []

    def test_hash_inside_a_string_literal_is_preserved(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k AND y.tag = 'a#b'",
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_fully_commented_statement_yields_nothing(self, sql):
        joins, tables, _ = sql.analyze(
            "-- SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k"
        )
        assert joins == []
        assert tables == set()

    def test_commented_predicate_is_dropped_from_the_condition(self, sql):
        found = joins_of(
            sql,
            """
            SELECT 1 FROM a.t1 x JOIN b.t2 y
              ON x.k = y.k
              -- AND x.dead = y.dead
            """,
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_trailing_comment_on_a_live_line(self, sql):
        found = joins_of(
            sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k -- the good one"
        )
        assert found[0].condition == "a.t1.k = b.t2.k"


class TestPythonComments:
    def test_hash_commented_join_is_ignored(self, pyspark):
        found = joins_of(
            pyspark,
            """
            a = spark.table("x.t1")
            b = spark.table("y.t2")
            # dead = a.join(b, a.k == b.k)
            live = a.join(b, a.j == b.j)
            """,
        )
        assert len(found) == 1
        assert found[0].condition == "x.t1.j = y.t2.j"

    def test_hash_commented_spark_sql_is_ignored(self, pyspark):
        found = joins_of(
            pyspark,
            """
            # spark.sql("SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k")
            spark.sql("SELECT 1 FROM c.t3 p JOIN d.t4 q ON p.k = q.k")
            """,
        )
        assert len(found) == 1
        assert (found[0].left.key, found[0].right.key) == ("c.t3", "d.t4")

    def test_block_commented_out_with_triple_quotes_is_ignored(self, pyspark):
        """A bare string statement is a docstring or a commented-out block."""
        found = joins_of(
            pyspark,
            '''
            a = spark.table("x.t1")
            """
            SELECT 1 FROM sales.orders o JOIN crm.customers c ON o.cid = c.id
            """
            '''
        )
        assert found == []

    def test_module_docstring_sql_is_ignored(self, pyspark):
        found = joins_of(
            pyspark,
            '''
            """Example, for reference only.

            SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k
            """
            import os
            '''
        )
        assert found == []

    def test_function_docstring_sql_is_ignored(self, pyspark):
        found = joins_of(
            pyspark,
            '''
            def build():
                """Replaces: SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k"""
                return None
            '''
        )
        assert found == []


class TestEmbeddedSql:
    """``spark.sql(""" '"""' """...""" '"""' """)`` is the common spelling."""

    def test_triple_quoted_spark_sql(self, pyspark):
        found = joins_of(
            pyspark,
            '''
            df = spark.sql("""
                SELECT o.id, c.name
                FROM sales.orders o
                JOIN crm.customers c ON o.cid = c.id
            """)
            '''
        )
        assert len(found) == 1
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_triple_quoted_with_internal_comments(self, pyspark):
        found = joins_of(
            pyspark,
            '''
            df = spark.sql("""
                SELECT 1
                FROM sales.orders o
                -- JOIN crm.customers c ON o.cid = c.id
                # JOIN crm.regions r ON o.rid = r.id
                JOIN sales.items i ON o.id = i.oid
            """)
            '''
        )
        pairs = {(j.left.key, j.right.key) for j in found}
        assert pairs == {("sales.items", "sales.orders")}

    def test_triple_quoted_cte(self, pyspark):
        found = joins_of(
            pyspark,
            '''
            df = spark.sql("""
                WITH recent AS (
                    SELECT o.id, o.cid FROM sales.orders o WHERE o.d > 1
                )
                SELECT 1 FROM recent r JOIN crm.customers c ON r.cid = c.id
            """)
            '''
        )
        assert (found[0].left.key, found[0].right.key) == ("crm.customers", "sales.orders")

    def test_sql_assigned_to_a_constant_is_still_found(self, pyspark):
        """Assigned SQL is live code, unlike a bare string statement."""
        found = joins_of(
            pyspark,
            '''
            QUERY = """
                SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k
            """
            spark.sql(QUERY)
            '''
        )
        assert len(found) == 1
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_sql_passed_directly_as_an_argument_is_found(self, pyspark):
        found = joins_of(
            pyspark,
            'run("""SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k""")',
        )
        assert len(found) == 1

    @pytest.mark.parametrize("method", ["spark.sql", "session.sql", "self.spark.sql"])
    def test_sql_via_any_receiver(self, pyspark, method):
        found = joins_of(
            pyspark, '%s("""SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k""")' % method
        )
        assert len(found) == 1


def test_notebook_hash_comment_is_ignored(notebook):
    import json

    document = json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [
                        'a = spark.table("x.t1")\n',
                        'b = spark.table("y.t2")\n',
                        "# dead = a.join(b, a.k == b.k)\n",
                        "live = a.join(b, a.j == b.j)\n",
                    ],
                    "outputs": [],
                    "execution_count": None,
                    "metadata": {},
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )
    joins, _, errors = notebook.analyze(document)
    assert errors == []
    assert len(joins) == 1
    assert joins[0].condition == "x.t1.j = y.t2.j"
