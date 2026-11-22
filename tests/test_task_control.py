import sqlite3

from fastapi.testclient import TestClient

from app.db import Database
from app.engine import Engine, Interrupted
from app.main import create_app
from app.task_control import rate_status, timing

pytest_plugins = ["test_service"]


def test_rolling_limit_survives_reopen_and_counts_retries(tmp_path):
    db = Database(tmp_path / "tasks.db")
    db.set("task_policy", {"enabled": True, "interval_seconds": 10, "max_per_hour": 2})
    for t in [100, 110]:
        db.execute("INSERT INTO job_runs(job_id,started,outcome) VALUES (?,?,?)", ("same-job", t, "failed"))
    db = Database(tmp_path / "tasks.db")
    assert rate_status(db, 115)["next_allowed_at"] == 3700
    assert rate_status(db, 3700)["wait_seconds"] == 0
    db.set("task_policy", {"enabled": False, "interval_seconds": 10, "max_per_hour": 1})
    assert rate_status(db, 115)["wait_seconds"] == 0


def test_interval_and_live_elapsed(tmp_path):
    db = Database(tmp_path / "tasks.db")
    db.execute("INSERT INTO job_runs(job_id,started) VALUES (?,?)", ("a", 100))
    assert rate_status(db, 102)["wait_seconds"] == 8
    assert (
        timing({"elapsed_seconds": 7, "state": "running", "run_started_at": 200, "started_at": 100, "timing_partial": 0}, 205)[
            "elapsed_seconds"
        ]
        == 12
    )


def test_retry_timing_excludes_wait_and_keeps_first_start(cfg, monkeypatch):
    import app.engine as module

    clock = [100]
    monkeypatch.setattr(module, "now", lambda: clock[0])
    e = Engine(cfg, Database(cfg.data / "db"), None)
    jid = e.enqueue("scan", {}, "计时")

    def interrupted(*args):
        clock[0] = 107
        raise Interrupted("重试")

    e.scan = interrupted
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    j = e.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert j["elapsed_seconds"] == 7 and j["finished_at"] is None

    def success(*args):
        clock[0] = 211
        return {}

    e.scan = success
    clock[0] = 200
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    j = e.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert (j["started_at"], j["finished_at"], j["elapsed_seconds"]) == (100, 211, 18)
    assert len(e.db.rows("SELECT * FROM job_runs")) == 2


def test_legacy_timestamps_are_unknown(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    db.execute(
        "INSERT INTO jobs(id,unique_key,kind,payload,title,state,created,updated,attempts) VALUES ('old','old','scan','{}','旧任务','succeeded',10,99,1)"
    )
    with sqlite3.connect(path) as c:
        for name in ["started_at", "run_started_at", "finished_at", "elapsed_seconds", "timing_partial"]:
            c.execute("ALTER TABLE jobs DROP COLUMN " + name)
    db = Database(path)
    j = db.one("SELECT * FROM jobs WHERE id='old'")
    assert j["started_at"] is None and j["finished_at"] is None
    assert timing(j, 200) == {"elapsed_seconds": None, "timing_partial": True}


def test_policy_validation_summary_and_cancellation(cfg):
    app = create_app(cfg)
    with TestClient(app) as c:
        pw = (cfg.data / "initial-password.txt").read_text().strip()
        response = c.post("/api/login", json={"password": pw})
        c.headers["X-CSRF-Token"] = response.json()["csrf"]
        for value in [-1, 3601, True, 1.5]:
            assert (
                c.patch(
                    "/api/settings", json={"task_policy": {"enabled": True, "interval_seconds": value, "max_per_hour": 120}}
                ).status_code
                == 400
            )
        assert (
            c.patch("/api/settings", json={"task_policy": {"enabled": True, "interval_seconds": 20, "max_per_hour": 50}}).status_code == 200
        )
        jid = app.state.engine.enqueue("scan", {}, "样本")
        c.post(f"/api/jobs/{jid}/cancel")
        j = c.get(f"/api/jobs/{jid}").json()
        assert j["finished_at"] and j["started_at"] is None
        c.post(f"/api/jobs/{jid}/retry")
        assert c.get(f"/api/jobs/{jid}").json()["finished_at"] is None
        result = c.get("/api/jobs?state=failed").json()
        assert result["total"] == 0 and result["summary"]["total"] == 1
        assert result["rate_limit"]["interval_seconds"] == 20


def test_worker_obeys_limit_before_claiming(cfg, monkeypatch):
    import app.engine as module
    monkeypatch.setattr(module, 'now', lambda: 105)
    e = Engine(cfg, Database(cfg.data/'db'), None)
    e.enqueue('scan', {}, '等待限流')
    e.db.execute('INSERT INTO job_runs(job_id,started) VALUES (?,?)', ('older',100))
    e.schedule = lambda: None
    calls = []
    def run(job):
        calls.append(job['id'])
        e.stop.set()
    e.run_job = run
    monkeypatch.setattr(e.stop, 'wait', lambda *args: e.stop.set())
    e.loop()
    assert calls == []
    e.db.set('task_policy', {'enabled':False,'interval_seconds':10,'max_per_hour':120})
    e.stop.clear()
    e.loop()
    assert len(calls) == 1
