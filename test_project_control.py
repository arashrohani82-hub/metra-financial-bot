import sqlite3

from project_control import (
    create_project,
    dashboard,
    init_project_control,
    project_metrics,
    record_money,
    update_progress,
)


def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_project_control(db)
    return db


def test_project_financial_and_referral_metrics():
    db = connection()
    create_project(
        db, 123, "P26-101", "Inspection", "ABC Inc.", 10000,
        "Habitation", 15
    )
    update_progress(db, 123, "P26-101", 50)
    record_money(db, 123, "P26-101", "invoice", 2500)
    record_money(db, 123, "P26-101", "payment", 2500)

    project = db.execute("SELECT * FROM projects").fetchone()
    metrics = project_metrics(project)

    assert metrics["earned_value"] == 5000
    assert metrics["billing_gap"] == 2500
    assert metrics["referral_accrued"] == 375
    assert metrics["outstanding"] == 0


def test_dashboard_flags_financial_gap():
    db = connection()
    create_project(db, 123, "P26-102", "Plans", "Client", 20000)
    update_progress(db, 123, "P26-102", 50)

    data = dashboard(db, 123)

    assert data["active_count"] == 1
    assert data["billing_gap"] == 10000
    assert data["at_risk"] == 1


def test_referral_requires_name():
    db = connection()
    try:
        create_project(db, 123, "P26-103", "Report", "Client", 1000, None, 20)
    except ValueError as exc:
        assert "referrer" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
