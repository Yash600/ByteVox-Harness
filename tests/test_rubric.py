"""The rubric must be right before it is allowed to judge anything. Pure code, no model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import rubric  # noqa: E402


def ev(file, line=None, ok=True, quote="q"):
    return {"file": file, "line": line, "verified": ok, "quote": quote}


def f(title, ev_, sev=None, detail=""):
    return {"title": title, "detail": detail, "severity": sev, "evidence": ev_}


def test_code_recall_counts_only_grounded_findings_near_the_right_line():
    good = [f("SQL injection in get_user", [ev("app.py", 9)]), f("Hardcoded API key", [ev("app.py", 5)]),
            f("Bare except hides errors", [ev("utils.py", 12)])]
    s = rubric.score("code", good)
    assert s["points"]["recall"] == 2.5 and len(s["detail"]["found"]) == 3
    fake = [f("SQL injection", [ev("app.py", 9, ok=False)])]                    # fabricated quote
    assert rubric.score("code", fake)["recall"] == 0 and rubric.score("code", fake)["grounded"] == 0
    wrong_line = [f("SQL injection", [ev("app.py", 40)])]                       # right words, wrong place
    assert rubric.score("code", wrong_line)["recall"] == 0


def test_a_todo_marker_on_the_secret_line_is_not_credited_as_the_secret():
    s = rubric.score("code", [f("Unfinished work marker TODO", [ev("app.py", 5)])])
    assert "Hardcoded API key" not in s["detail"]["found"]


def test_triage_checks_urgency_per_ticket_and_the_highest_wins():
    ok = [f("Billing", [ev("ticket_01.txt")], "high"), f("Feature", [ev("ticket_02.txt")], "low"),
          f("Login", [ev("ticket_03.txt")], "critical")]
    assert rubric.score("triage", ok)["recall"] == 1.0
    mixed = [f("Billing", [ev("ticket_01.txt")], "high"), f("Billing again", [ev("ticket_01.txt")], "medium"),
             f("Feature", [ev("ticket_02.txt")], "medium"), f("Login", [ev("ticket_03.txt")], "high")]
    s = rubric.score("triage", mixed)
    assert s["recall"] == 2 / 3 and "ticket_02.txt" in s["detail"]["missed"]    # low ticket marked medium


def test_research_facts_need_grounded_evidence_and_count_sources():
    a = f("SQLite is serverless", [ev("web/01_x.txt", quote="SQLite is a serverless, self-contained engine")])
    b = f("Postgres is client-server", [ev("web/02_y.txt", quote="PostgreSQL uses a client/server model")])
    s = rubric.score("research", [a, b])
    assert s["recall"] == 2 / 5 and s["detail"]["distinct_sources"] == 2
    ungrounded = f("SQLite is serverless", [ev("web/01_x.txt", ok=False)])
    assert rubric.score("research", [ungrounded])["recall"] == 0


def test_distinctness_penalises_duplicates_but_not_distinct_issues_on_one_line():
    dup = [f("SQL injection in get_user", [ev("app.py", 9)]), f("SQL injection via concatenation in get_user", [ev("app.py", 9)])]
    assert rubric.score("code", dup)["distinct"] == 0.5
    two = [f("Hardcoded API key", [ev("app.py", 5)]), f("Unfinished work marker TODO", [ev("app.py", 5)])]
    assert rubric.score("code", two)["distinct"] == 1.0


def test_empty_results_score_zero_not_crash():
    for t in rubric.TASKS:
        s = rubric.score(t, [])
        assert s["total"] == 0 and s["n_findings"] == 0


if __name__ == "__main__":
    n = fail = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            n += 1
            try:
                fn()
                print("PASS ", name)
            except Exception as e:
                fail += 1
                print("FAIL ", name, repr(e))
    print(f"\n{n - fail}/{n} passed")
    sys.exit(1 if fail else 0)
