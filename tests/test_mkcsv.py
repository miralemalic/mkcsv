#!/usr/bin/env python3
"""Tests for mkcsv. Standard library only -- run with:

    python3 -m unittest discover -s tests -v
"""

import contextlib
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mkcsv  # noqa: E402

mkcsv.QUIET = True  # keep the per-file report out of the test output


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mkcsv-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, text, encoding="utf-8"):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        return path

    def rows(self, name):
        """Parsed rows of an output CSV, header included."""
        path = os.path.join(self.tmp, name)
        with io.open(path, encoding="utf-8", newline="") as fh:
            return list(csv.reader(fh))

    def convert_sql(self, text, name="dump.sql", **kw):
        kw.setdefault("backslash_escapes", True)
        path = self.write(name, text)
        return mkcsv.convert_sql(path, self.tmp, "utf-8", kw["backslash_escapes"],
                                 kw.get("force", False), kw.get("split", 0))

    def convert_delimited(self, text, name="data.txt", **kw):
        path = self.write(name, text, kw.get("encoding_write", "utf-8"))
        return mkcsv.convert_delimited(
            path, self.tmp, kw.get("delimiter"), kw.get("encoding", "utf-8"),
            kw.get("force", False), kw.get("split", 0),
            kw.get("no_header", False), kw.get("no_quote", False))

    def convert_aligned(self, text, name="query.txt", **kw):
        path = self.write(name, text)
        return mkcsv.convert_aligned(path, self.tmp, "utf-8",
                                     kw.get("force", False), kw.get("split", 0))


# --------------------------------------------------------------- SQL INSERT

class TestSqlInsert(Base):
    def test_columns_come_from_create_table(self):
        self.convert_sql(
            "CREATE TABLE leads (id INT, email VARCHAR(255), city TEXT);\n"
            "INSERT INTO leads VALUES (1,'a@b.com','Austin');\n")
        self.assertEqual(self.rows("leads.csv"),
                         [["id", "email", "city"], ["1", "a@b.com", "Austin"]])

    def test_key_and_constraint_lines_are_not_columns(self):
        self.convert_sql(
            "CREATE TABLE t (\n"
            "  id INT NOT NULL,\n"
            "  name VARCHAR(50),\n"
            "  PRIMARY KEY (id),\n"
            "  UNIQUE KEY name_idx (name),\n"
            "  CONSTRAINT fk FOREIGN KEY (id) REFERENCES o(id)\n"
            ");\n"
            "INSERT INTO t VALUES (1,'x');\n")
        self.assertEqual(self.rows("t.csv")[0], ["id", "name"])

    def test_decimal_default_inside_column_def_is_not_a_split(self):
        self.convert_sql(
            "CREATE TABLE t (id INT, amount DECIMAL(10,2), note TEXT);\n"
            "INSERT INTO t VALUES (1,9.99,'ok');\n")
        self.assertEqual(self.rows("t.csv")[0], ["id", "amount", "note"])

    def test_explicit_column_list_wins(self):
        self.convert_sql("INSERT INTO t (email, city) VALUES ('a@b.com','Austin');\n")
        self.assertEqual(self.rows("t.csv"),
                         [["email", "city"], ["a@b.com", "Austin"]])

    def test_synthetic_header_when_schema_unknown(self):
        self.convert_sql("INSERT INTO t VALUES (1,2,3);\n")
        self.assertEqual(self.rows("t.csv")[0], ["col1", "col2", "col3"])

    def test_multi_row_values(self):
        self.convert_sql("INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'c');\n")
        self.assertEqual(self.rows("t.csv")[1:],
                         [["1", "a"], ["2", "b"], ["3", "c"]])

    def test_doubled_quote_is_an_escaped_quote(self):
        self.convert_sql("INSERT INTO t VALUES ('It''s here','ok');\n")
        self.assertEqual(self.rows("t.csv")[1], ["It's here", "ok"])

    def test_backslash_escapes(self):
        self.convert_sql(r"INSERT INTO t VALUES ('It\'s','a\tb','x\\y');" + "\n")
        self.assertEqual(self.rows("t.csv")[1], ["It's", "a\tb", "x\\y"])

    def test_no_backslash_escapes_keeps_them_literal(self):
        self.convert_sql(r"INSERT INTO t VALUES ('C:\path');" + "\n",
                         backslash_escapes=False)
        self.assertEqual(self.rows("t.csv")[1], [r"C:\path"])

    def test_null_becomes_empty(self):
        self.convert_sql("INSERT INTO t VALUES (1,NULL,'x');\n")
        self.assertEqual(self.rows("t.csv")[1], ["1", "", "x"])

    def test_semicolon_inside_string_does_not_end_statement(self):
        self.convert_sql("INSERT INTO t VALUES ('a;b','c');\n")
        self.assertEqual(self.rows("t.csv")[1], ["a;b", "c"])

    def test_paren_inside_string_does_not_end_row(self):
        self.convert_sql("INSERT INTO t VALUES ('Smith (Jr)','x');\n")
        self.assertEqual(self.rows("t.csv")[1], ["Smith (Jr)", "x"])

    def test_comments_are_stripped(self):
        self.convert_sql(
            "-- a line comment with 'a quote\n"
            "# a mysql comment\n"
            "/* a block\n   comment */\n"
            "INSERT INTO t VALUES (1,'x');\n")
        self.assertEqual(self.rows("t.csv")[1], ["1", "x"])

    def test_backtick_identifiers(self):
        self.convert_sql("INSERT INTO `my db`.`t` (`a`,`b`) VALUES (1,2);\n")
        self.assertEqual(self.rows("t.csv"), [["a", "b"], ["1", "2"]])

    def test_qualified_name_keeps_only_the_table(self):
        self.convert_sql("INSERT INTO `db`.`t` VALUES (1);\n")
        self.assertIn("t.csv", os.listdir(self.tmp))
        self.assertNotIn("`t.csv", os.listdir(self.tmp))

    def test_bracket_quoted_name_with_a_space(self):
        self.convert_sql("INSERT INTO [dbo].[My Table] VALUES (1);\n")
        self.assertEqual(self.rows("My Table.csv")[1], ["1"])

    def test_schema_qualified_create_table_matches_the_insert(self):
        self.convert_sql("CREATE TABLE public.leads (id INT, email TEXT);\n"
                         "INSERT INTO public.leads VALUES (1,'a@b.com');\n")
        self.assertEqual(self.rows("leads.csv")[0], ["id", "email"])

    def test_final_statement_without_trailing_semicolon(self):
        self.convert_sql("INSERT INTO t VALUES (1,'x')")
        self.assertEqual(self.rows("t.csv")[1], ["1", "x"])

    def test_multiple_tables_go_to_separate_files(self):
        self.convert_sql(
            "INSERT INTO a VALUES (1);\nINSERT INTO b VALUES (2);\n")
        self.assertEqual(self.rows("a.csv")[1], ["1"])
        self.assertEqual(self.rows("b.csv")[1], ["2"])

    def test_short_row_is_padded_and_long_row_truncated(self):
        self.convert_sql(
            "CREATE TABLE t (a INT, b INT, c INT);\n"
            "INSERT INTO t VALUES (1,2,3);\n"
            "INSERT INTO t VALUES (4,5);\n"
            "INSERT INTO t VALUES (6,7,8,9);\n")
        self.assertEqual(self.rows("t.csv")[1:],
                         [["1", "2", "3"], ["4", "5", ""], ["6", "7", "8"]])

    def test_no_data_reports_nothing_written(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.convert_sql("SELECT 1;\n"), 0)


# ------------------------------------------------------ the chunk boundary

class TestChunkBoundary(Base):
    """iter_sql reads in CHUNK-sized blocks. A quote, comment or statement
    straddling a block boundary must parse the same as one that doesn't --
    shrinking CHUNK sweeps the boundary across every byte offset."""

    DUMP = (
        "CREATE TABLE t (id INT, note TEXT, tag TEXT);\n"
        "-- comment with 'an apostrophe and a ; semicolon\n"
        "INSERT INTO t VALUES (1,'It''s a test; really','a');\n"
        "/* block comment ' with quote */\n"
        "INSERT INTO t VALUES (2,'plain','b'),(3,'has (parens)','c');\n"
        r"INSERT INTO t VALUES (4,'back\'slash','d');" + "\n"
        "INSERT INTO t VALUES (5,NULL,'e');\n"
    )

    def expected(self):
        return [["id", "note", "tag"],
                ["1", "It's a test; really", "a"],
                ["2", "plain", "b"],
                ["3", "has (parens)", "c"],
                ["4", "back'slash", "d"],
                ["5", "", "e"]]

    def test_identical_output_at_every_chunk_size(self):
        original = mkcsv.CHUNK
        self.addCleanup(setattr, mkcsv, "CHUNK", original)
        # primes and awkward sizes, so boundaries land mid-token everywhere
        for size in (1, 2, 3, 7, 13, 17, 31, 64, 97, 128, 251, original):
            with self.subTest(chunk=size):
                for f in os.listdir(self.tmp):
                    os.remove(os.path.join(self.tmp, f))
                mkcsv.CHUNK = size
                self.convert_sql(self.DUMP)
                self.assertEqual(self.rows("t.csv"), self.expected())

    def test_copy_block_survives_small_chunks(self):
        original = mkcsv.CHUNK
        self.addCleanup(setattr, mkcsv, "CHUNK", original)
        dump = ("COPY t (a, b) FROM stdin;\n"
                "1\tone\n2\t\\N\n3\ttab\\there\n\\.\n")
        for size in (1, 3, 8, 29, original):
            with self.subTest(chunk=size):
                for f in os.listdir(self.tmp):
                    os.remove(os.path.join(self.tmp, f))
                mkcsv.CHUNK = size
                self.convert_sql(dump)
                self.assertEqual(
                    self.rows("t.csv"),
                    [["a", "b"], ["1", "one"], ["2", ""], ["3", "tab\there"]])


# ------------------------------------------------------- PostgreSQL COPY

class TestPgCopy(Base):
    def test_tab_delimited_with_nulls(self):
        self.convert_sql("COPY public.leads (id, email) FROM stdin;\n"
                         "1\ta@b.com\n2\t\\N\n\\.\n")
        self.assertEqual(self.rows("leads.csv"),
                         [["id", "email"], ["1", "a@b.com"], ["2", ""]])

    def test_escapes_are_decoded(self):
        self.convert_sql("COPY t (a) FROM stdin;\n"
                         "line\\nbreak\n" "tab\\there\n" "back\\\\slash\n" "\\.\n")
        self.assertEqual([r[0] for r in self.rows("t.csv")[1:]],
                         ["line\nbreak", "tab\there", "back\\slash"])

    def test_csv_variant_with_explicit_delimiter(self):
        self.convert_sql("COPY t (a, b) FROM stdin WITH (FORMAT CSV, DELIMITER ',');\n"
                         'x,"has,comma"\n\\.\n')
        self.assertEqual(self.rows("t.csv")[1], ["x", "has,comma"])

    def test_data_stops_at_terminator(self):
        self.convert_sql("COPY t (a) FROM stdin;\n1\n\\.\n"
                         "INSERT INTO other VALUES ('z');\n")
        self.assertEqual(self.rows("t.csv")[1:], [["1"]])
        self.assertEqual(self.rows("other.csv")[1], ["z"])


# ---------------------------------------------------------- delimited text

class TestDelimited(Base):
    def test_sniffs_tab(self):
        self.convert_delimited("a\tb\n1\t2\n")
        self.assertEqual(self.rows("data.csv"), [["a", "b"], ["1", "2"]])

    def test_sniffs_pipe(self):
        self.convert_delimited("a|b|c\n1|2|3\n4|5|6\n")
        self.assertEqual(self.rows("data.csv")[0], ["a", "b", "c"])

    def test_sniffs_semicolon(self):
        self.convert_delimited("a;b\n1;2\n3;4\n")
        self.assertEqual(self.rows("data.csv")[0], ["a", "b"])

    def test_explicit_delimiter_overrides_sniffing(self):
        self.convert_delimited("a:b\n1:2\n", delimiter=":")
        self.assertEqual(self.rows("data.csv"), [["a", "b"], ["1", "2"]])

    def test_bom_is_stripped(self):
        self.convert_delimited("a\tb\n1\t2\n", encoding_write="utf-8-sig")
        self.assertEqual(self.rows("data.csv")[0][0], "a")

    def test_ragged_rows_are_preserved_not_padded(self):
        self.convert_delimited("a\tb\tc\n1\t2\n3\t4\t5\t6\n")
        self.assertEqual(self.rows("data.csv")[1:], [["1", "2"], ["3", "4", "5", "6"]])

    def test_quoted_field_containing_delimiter(self):
        self.convert_delimited('a,b\n"x,y",z\n', delimiter=",")
        self.assertEqual(self.rows("data.csv")[1], ["x,y", "z"])

    def test_no_quote_treats_quotes_literally(self):
        # a stray quote would otherwise swallow the following lines
        self.convert_delimited('a\tb\n5" pipe\tx\nnext\ty\n', no_quote=True)
        self.assertEqual(self.rows("data.csv")[1:], [['5" pipe', "x"], ["next", "y"]])

    def test_blank_lines_are_skipped(self):
        self.convert_delimited("a\tb\n\n1\t2\n\n3\t4\n")
        self.assertEqual(len(self.rows("data.csv")), 3)


# -------------------------------------------------------- aligned consoles

class TestAligned(Base):
    def test_psql_table(self):
        self.convert_aligned(
            " id |  email      \n"
            "----+-------------\n"
            "  1 | a@b.com     \n"
            "  2 | c@d.com     \n"
            "(2 rows)\n")
        self.assertEqual(self.rows("query.csv"),
                         [["id", "email"], ["1", "a@b.com"], ["2", "c@d.com"]])

    def test_mysql_box(self):
        self.convert_aligned(
            "+----+---------+\n"
            "| id | name    |\n"
            "+----+---------+\n"
            "|  1 | Ada     |\n"
            "+----+---------+\n")
        self.assertEqual(self.rows("query.csv"), [["id", "name"], ["1", "Ada"]])

    def test_markdown_table(self):
        self.convert_aligned(
            "| a | b |\n|:--|--:|\n| 1 | 2 |\n")
        self.assertEqual(self.rows("query.csv"), [["a", "b"], ["1", "2"]])

    def test_banner_lines_before_the_header_are_skipped(self):
        self.convert_aligned(
            "Running query...\n"
            " id | name \n"
            "----+------\n"
            "  1 | Ada  \n")
        self.assertEqual(self.rows("query.csv")[0], ["id", "name"])

    def test_short_row_padded_to_header_width(self):
        self.convert_aligned(
            " a | b | c \n---+---+---\n 1 | 2 \n")
        self.assertEqual(self.rows("query.csv")[1], ["1", "2", ""])


# ------------------------------------------------------- format detection

class TestDetectFormat(Base):
    def detect(self, name, text):
        return mkcsv.detect_format(self.write(name, text), "utf-8")

    def test_sql_content_in_a_txt_file(self):
        self.assertEqual(self.detect("x.txt", "INSERT INTO t VALUES (1);"), "sql")

    def test_copy_dump_is_sql(self):
        self.assertEqual(self.detect("x.txt", "COPY t (a) FROM stdin;\n1\n\\.\n"), "sql")

    def test_console_output_in_a_sql_file(self):
        self.assertEqual(
            self.detect("x.sql", " a | b \n---+---\n 1 | 2 \n(1 row)\n"), "aligned")

    def test_plain_tsv_is_delimited(self):
        self.assertEqual(self.detect("x.txt", "a\tb\n1\t2\n"), "delimited")

    def test_pipe_delimited_without_a_rule_is_not_aligned(self):
        self.assertEqual(self.detect("x.txt", "a|b\n1|2\n3|4\n"), "delimited")


# ---------------------------------------------------------------- splitting

class TestSplit(Base):
    def test_parts_repeat_the_header_and_partition_the_rows(self):
        body = "".join(f"{i}\tvalue{i}\n" for i in range(1, 11))
        self.convert_delimited("id\tname\n" + body, split=4)
        parts = [f"data - part {i:03d}.csv" for i in (1, 2, 3)]
        for name in parts:
            self.assertEqual(self.rows(name)[0], ["id", "name"])
        counts = [len(self.rows(n)) - 1 for n in parts]
        self.assertEqual(counts, [4, 4, 2])
        seen = [r[0] for n in parts for r in self.rows(n)[1:]]
        self.assertEqual(seen, [str(i) for i in range(1, 11)])

    def test_no_header_option_keeps_the_first_row_as_data(self):
        self.convert_delimited("1\ta\n2\tb\n3\tc\n", split=2, no_header=True)
        first = self.rows("data - part 001.csv")
        self.assertEqual(first[0], ["1", "a"])

    def test_sql_split_repeats_the_header(self):
        self.convert_sql(
            "CREATE TABLE t (a INT);\n"
            + "".join(f"INSERT INTO t VALUES ({i});\n" for i in range(6)),
            split=4)
        self.assertEqual(self.rows("t - part 001.csv")[0], ["a"])
        self.assertEqual(self.rows("t - part 002.csv")[0], ["a"])
        self.assertEqual(len(self.rows("t - part 002.csv")) - 1, 2)


# ------------------------------------------------------------- the Out sink

class TestOut(Base):
    def test_row_count_is_not_fooled_by_a_newline_inside_a_field(self):
        out = mkcsv.Out(self.tmp, "t", ["a", "b"], 0, False)
        out.writerow(["x", "line one\nline two"])
        out.writerow(["y", "z"])
        out.close()
        self.assertEqual(out.counts, [2])            # two logical rows
        with io.open(out.paths[0], encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 4)  # but four physical lines

    def test_counts_are_per_part(self):
        out = mkcsv.Out(self.tmp, "t", ["a"], 2, False)
        for i in range(5):
            out.writerow([i])
        out.close()
        self.assertEqual(out.counts, [2, 2, 1])

    def test_existing_output_is_not_clobbered(self):
        self.write("t.csv", "keep me\n")
        with self.assertRaises(FileExistsError):
            mkcsv.Out(self.tmp, "t", ["a"], 0, False)
        with io.open(os.path.join(self.tmp, "t.csv"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep me\n")

    def test_force_overwrites(self):
        self.write("t.csv", "old\n")
        out = mkcsv.Out(self.tmp, "t", ["a"], 0, True)
        out.writerow(["new"])
        out.close()
        self.assertEqual(self.rows("t.csv"), [["a"], ["new"]])


# ------------------------------------------------------------------ helpers

class TestHelpers(Base):
    def test_human_units(self):
        self.assertEqual(mkcsv.human(512), "512B")
        self.assertEqual(mkcsv.human(1536), "1.5KB")
        self.assertEqual(mkcsv.human(5 << 20), "5.0MB")

    def test_free_space_is_cross_platform(self):
        # shutil.disk_usage replaces the POSIX-only os.statvfs
        self.assertIsInstance(mkcsv.free_space(self.tmp), int)

    def test_free_space_returns_none_for_a_bad_path(self):
        self.assertIsNone(mkcsv.free_space(os.path.join(self.tmp, "nope")))

    def test_sniff_delimiter_prefers_the_consistent_candidate(self):
        # commas appear inside the prose, but every line has exactly one pipe
        sample = "name|note\nAda|born in London, England\nAlan|solved, eventually\n"
        self.assertEqual(mkcsv.sniff_delimiter(sample), "|")

    def test_table_name_unquotes_the_last_component(self):
        self.assertEqual(mkcsv._table_name("`db`.`t`"), "t")
        self.assertEqual(mkcsv._table_name("public.leads"), "leads")
        self.assertEqual(mkcsv._table_name("[dbo].[My Table]"), "My Table")
        self.assertEqual(mkcsv._table_name('"Odd.Name"'), "Odd.Name")
        self.assertEqual(mkcsv._table_name("plain"), "plain")

    def test_table_name_cannot_escape_the_output_directory(self):
        # a downloaded dump should not be able to pick the write path
        self.assertNotIn("/", mkcsv._table_name("`../../etc/passwd`"))
        self.assertEqual(mkcsv._table_name("`..`"), "table")

    def test_is_divider(self):
        for line in ("---+---", "+----+----+", "====", "| :--- | ---: |"):
            self.assertTrue(mkcsv._is_divider(line), line)
        for line in ("a | b", "", "12"):
            self.assertFalse(mkcsv._is_divider(line), line)


if __name__ == "__main__":
    unittest.main()
