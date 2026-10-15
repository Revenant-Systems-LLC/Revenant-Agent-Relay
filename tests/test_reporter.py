from src.reporter import subject_line


def _run(successes, simulated=0, status="COMPLETE", goal=3):
    run = {"successes": successes, "daily_goal": goal, "final_status": status}
    if simulated:
        run["simulated_successes"] = simulated
    return run


def test_subject_line_unchanged_when_nothing_simulated():
    assert subject_line(_run(3)) == "Revenant Relay - COMPLETE - 3/3 Posted"


def test_subject_line_all_simulated_never_reads_complete():
    subject = subject_line(_run(3, simulated=3))
    assert subject == "Revenant Relay - SIMULATED - 0/3 Posted (3 simulated)"
    assert "COMPLETE" not in subject


def test_subject_line_mixed_counts_real_posts_only():
    subject = subject_line(_run(3, simulated=2, status="PARTIAL"))
    assert subject == "Revenant Relay - PARTIAL - 1/3 Posted (2 simulated)"


def test_subject_line_honours_prefix():
    assert subject_line(_run(1, status="PARTIAL"), prefix="Relay Dev") == "Relay Dev - PARTIAL - 1/3 Posted"
