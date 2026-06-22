from src.progress_parser import ProgressParser


def test_parse_suite():
    p = ProgressParser()
    p.parse_line("##e2e## suite versioned-upgrade 11")
    assert p.progress.total == 11
    assert p.progress.phase == "testing"


def test_parse_case():
    p = ProgressParser()
    p.parse_line("##e2e## case Fresh install")
    assert p.progress.current_test == "Fresh install"


def test_parse_pass():
    p = ProgressParser()
    p.parse_line("##e2e## pass Fresh install")
    assert p.progress.passed == 1


def test_parse_fail():
    p = ProgressParser()
    p.parse_line("##e2e## fail SHA-256 rejection")
    assert p.progress.failed == 1


def test_parse_summary():
    p = ProgressParser()
    p.parse_line("##e2e## summary 10 1 11")
    assert p.progress.passed == 10
    assert p.progress.failed == 1
    assert p.progress.total == 11
    assert p.progress.phase == "completed"


def test_mixed_output():
    p = ProgressParser()
    lines = [
        "##e2e## suite test-suite 3",
        "some random log output",
        "\033[1m=== test section ===\033[0m",
        "##e2e## case test-one",
        "  PASS test-one (old format, ignored by parser)",
        "##e2e## pass test-one",
        "##e2e## case test-two",
        "##e2e## fail test-two",
        "##e2e## case test-three",
        "##e2e## pass test-three",
        "##e2e## summary 2 1 3",
    ]
    for line in lines:
        p.parse_line(line)

    assert p.progress.passed == 2
    assert p.progress.failed == 1
    assert p.progress.total == 3
    assert p.progress.phase == "completed"


def test_no_markers():
    p = ProgressParser()
    p.parse_line("regular output line")
    p.parse_line("  PASS some test")
    p.parse_line("  FAIL another test")

    assert p.progress.passed == 0
    assert p.progress.failed == 0
    assert p.progress.total == 0
    assert p.progress.phase == "pending"


def test_malformed_marker():
    p = ProgressParser()
    p.parse_line("##e2e## unknown_command arg")
    p.parse_line("##e2e##")
    p.parse_line("##e2e## suite")  # no count
    assert p.progress.phase == "testing"  # suite still sets phase


def test_incremental_counting():
    p = ProgressParser()
    p.parse_line("##e2e## pass a")
    p.parse_line("##e2e## pass b")
    p.parse_line("##e2e## pass c")
    p.parse_line("##e2e## fail d")
    assert p.progress.passed == 3
    assert p.progress.failed == 1


def test_to_dict():
    p = ProgressParser()
    p.parse_line("##e2e## suite my-suite 5")
    p.parse_line("##e2e## pass test-1")
    p.progress.elapsed_seconds = 12.345

    d = p.progress.to_dict()
    assert d["phase"] == "testing"
    assert d["passed"] == 1
    assert d["total"] == 5
    assert d["elapsed_seconds"] == 12.3
