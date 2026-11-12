"""The Extractor's SQL guard: nothing the model writes reaches MySQL unchecked."""
import json
import os

import pytest

import config
from agents import Extractor
from conftest import FakePieas, STUDENT_SCHEMA

validate = Extractor._validate


@pytest.mark.parametrize("phase", ["bulk", "incremental", "deletions"])
def test_hand_written_default_sql_passes_its_own_guard(phase):
    validate(Extractor._default_sql("students", phase), "students", phase)


@pytest.mark.parametrize("sql, reason", [
    ("", "empty"),
    ("SELECT * FROM students; DROP TABLE students", "multiple statements"),
    ("DELETE FROM students", "not a SELECT"),
    ("SELECT * FROM students WHERE id IN (SELECT id FROM mysql.user)", "forbidden keyword"),
    ("SELECT * FROM students INTO OUTFILE '/tmp/x'", "forbidden keyword"),
    ("SELECT table_name FROM information_schema.tables", "forbidden keyword"),
    ("SELECT * FROM students JOIN payroll ON 1=1", "table not allowed"),
    ("SELECT * FROM courses", "does not read students"),
])
def test_unsafe_or_off_target_sql_is_rejected(sql, reason):
    with pytest.raises(ValueError, match=reason):
        validate(sql, "students", "bulk")


@pytest.mark.parametrize("keyword", ["insert", "update", "drop", "alter", "truncate", "grant"])
def test_write_keywords_are_rejected_even_inside_a_select(keyword):
    with pytest.raises(ValueError, match="forbidden keyword"):
        validate(f"SELECT * FROM students WHERE note = '{keyword} me'", "students", "bulk")


def test_incremental_sql_needs_exactly_one_placeholder():
    with pytest.raises(ValueError, match="exactly one"):
        validate("SELECT * FROM students", "students", "incremental")
    with pytest.raises(ValueError, match="exactly one"):
        validate("SELECT * FROM students WHERE last_updated > %s AND id > %s",
                 "students", "incremental")


def test_bulk_and_deletion_sql_take_no_parameters():
    for phase in ("bulk", "deletions"):
        with pytest.raises(ValueError, match="takes no parameters"):
            validate("SELECT * FROM students WHERE last_updated > %s", "students", phase)


def test_a_hand_quoted_placeholder_is_rejected():
    # The driver quotes parameters itself; a quoted %s becomes a syntax error.
    with pytest.raises(ValueError, match="must not be quoted"):
        validate("SELECT * FROM students WHERE last_updated > '%s'", "students", "incremental")


def make_extractor():
    return Extractor(FakePieas({"students": []}, {"students": STUDENT_SCHEMA}))


def test_default_sql_is_used_and_cached_when_the_llm_is_unreachable(llm):
    extractor = make_extractor()

    sql = extractor.sql_for("students", "bulk", "Fetch every row.")

    assert sql == "SELECT * FROM students ORDER BY id"
    with open(os.path.join(config.PLAN_CACHE, "sql_students_bulk.json")) as fh:
        assert json.load(fh)["source"] == "fallback"

    extractor.sql_for("students", "bulk", "Fetch every row.")
    assert len(llm.prompts) == 1, "the cached plan must not cost a second LLM call"


def test_valid_generated_sql_is_accepted(llm):
    llm.responder = lambda prompt: {"sql": "SELECT * FROM students WHERE last_updated > %s;"}

    sql = make_extractor().sql_for("students", "incremental", "Fetch changed rows.")

    assert sql == "SELECT * FROM students WHERE last_updated > %s"


def test_rejected_sql_is_sent_back_once_then_replaced_by_the_default(llm):
    llm.responder = lambda prompt: {"sql": "DROP TABLE students"}

    sql = make_extractor().sql_for("students", "deletions", "Fetch ids.")

    assert sql == "SELECT id FROM students"
    assert len(llm.prompts) == 2
    assert "previous answer was rejected" in llm.prompts[1]


def test_a_poisoned_cache_file_is_discarded(llm):
    os.makedirs(config.PLAN_CACHE)
    with open(os.path.join(config.PLAN_CACHE, "sql_students_bulk.json"), "w") as fh:
        json.dump({"sql": "DELETE FROM students", "source": "gemini"}, fh)

    sql = make_extractor().sql_for("students", "bulk", "Fetch every row.")

    assert sql == "SELECT * FROM students ORDER BY id"
