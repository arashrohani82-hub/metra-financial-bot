from datetime import datetime


PROJECT_STATUSES = ("active", "on_hold", "completed", "closed")


def init_project_control(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_code TEXT NOT NULL,
            title TEXT NOT NULL,
            client_name TEXT NOT NULL,
            contract_value REAL NOT NULL DEFAULT 0,
            technical_progress REAL NOT NULL DEFAULT 0,
            invoiced_amount REAL NOT NULL DEFAULT 0,
            collected_amount REAL NOT NULL DEFAULT 0,
            referrer_name TEXT,
            referral_rate REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            next_action TEXT,
            due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, project_code)
        );

        CREATE TABLE IF NOT EXISTS project_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            amount REAL,
            progress REAL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE INDEX IF NOT EXISTS idx_projects_user_status
        ON projects(user_id, status);
        """
    )


def _now():
    return datetime.utcnow().isoformat()


def _project(connection, user_id, project_code):
    return connection.execute(
        "SELECT * FROM projects WHERE user_id=? AND project_code=?",
        (user_id, project_code.upper().strip()),
    ).fetchone()


def create_project(
    connection,
    user_id,
    project_code,
    title,
    client_name,
    contract_value,
    referrer_name=None,
    referral_rate=0,
):
    project_code = project_code.upper().strip()
    contract_value = max(0.0, float(contract_value))
    referral_rate = max(0.0, min(100.0, float(referral_rate or 0)))
    if not project_code or not title.strip() or not client_name.strip():
        raise ValueError("project code, title and client are required")
    if referral_rate and not (referrer_name or "").strip():
        raise ValueError("referrer name is required when a referral rate is set")

    now = _now()
    cursor = connection.execute(
        """
        INSERT INTO projects(
            user_id, project_code, title, client_name, contract_value,
            referrer_name, referral_rate, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            project_code,
            title.strip(),
            client_name.strip(),
            contract_value,
            (referrer_name or "").strip() or None,
            referral_rate,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO project_events(project_id, event_type, note, created_at)
        VALUES (?, 'created', 'Project created', ?)
        """,
        (cursor.lastrowid, now),
    )
    return cursor.lastrowid


def update_progress(connection, user_id, project_code, progress, note=None):
    project = _project(connection, user_id, project_code)
    if not project:
        raise LookupError("project not found")
    progress = max(0.0, min(100.0, float(progress)))
    now = _now()
    connection.execute(
        "UPDATE projects SET technical_progress=?, updated_at=? WHERE id=?",
        (progress, now, project["id"]),
    )
    connection.execute(
        """
        INSERT INTO project_events(project_id, event_type, progress, note, created_at)
        VALUES (?, 'progress', ?, ?, ?)
        """,
        (project["id"], progress, note, now),
    )


def record_money(connection, user_id, project_code, event_type, amount, note=None):
    if event_type not in {"invoice", "payment"}:
        raise ValueError("event_type must be invoice or payment")
    project = _project(connection, user_id, project_code)
    if not project:
        raise LookupError("project not found")
    amount = float(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    field = "invoiced_amount" if event_type == "invoice" else "collected_amount"
    now = _now()
    connection.execute(
        f"UPDATE projects SET {field}={field}+?, updated_at=? WHERE id=?",
        (amount, now, project["id"]),
    )
    connection.execute(
        """
        INSERT INTO project_events(project_id, event_type, amount, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project["id"], event_type, amount, note, now),
    )


def list_projects(connection, user_id, status="active"):
    return connection.execute(
        """
        SELECT * FROM projects
        WHERE user_id=? AND status=?
        ORDER BY updated_at DESC, id DESC
        """,
        (user_id, status),
    ).fetchall()


def get_project(connection, user_id, project_code):
    return _project(connection, user_id, project_code)


def project_metrics(project):
    contract = float(project["contract_value"] or 0)
    invoiced = float(project["invoiced_amount"] or 0)
    collected = float(project["collected_amount"] or 0)
    progress = float(project["technical_progress"] or 0)
    rate = float(project["referral_rate"] or 0)
    earned_value = contract * progress / 100
    referral_accrued = collected * rate / 100
    billing_gap = max(0.0, earned_value - invoiced)
    return {
        "contract": contract,
        "invoiced": invoiced,
        "collected": collected,
        "outstanding": max(0.0, invoiced - collected),
        "remaining_contract": max(0.0, contract - invoiced),
        "progress": progress,
        "earned_value": earned_value,
        "billing_gap": billing_gap,
        "referral_accrued": referral_accrued,
    }


def dashboard(connection, user_id):
    projects = list_projects(connection, user_id)
    metrics = [project_metrics(project) for project in projects]
    contract = sum(item["contract"] for item in metrics)
    invoiced = sum(item["invoiced"] for item in metrics)
    collected = sum(item["collected"] for item in metrics)
    outstanding = sum(item["outstanding"] for item in metrics)
    billing_gap = sum(item["billing_gap"] for item in metrics)
    commissions = sum(item["referral_accrued"] for item in metrics)
    at_risk = sum(
        1
        for item in metrics
        if item["billing_gap"] > max(250.0, item["contract"] * 0.1)
        or item["outstanding"] > max(250.0, item["contract"] * 0.1)
    )
    return {
        "active_count": len(projects),
        "contract": contract,
        "invoiced": invoiced,
        "collected": collected,
        "outstanding": outstanding,
        "billing_gap": billing_gap,
        "commissions": commissions,
        "at_risk": at_risk,
    }
