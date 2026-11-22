import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Database, now
from app.engine import Engine
from app.main import create_app
from app.personal import Personal

pytest_plugins = ["test_service"]


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    assert condition()


def test_exclusive_database_lock_does_not_kill_scheduler(cfg, monkeypatch):
    original_connect = sqlite3.connect

    def fast_connect(*args, **kwargs):
        kwargs["timeout"] = 0.05
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fast_connect)
    database = Database(cfg.data / "locked.sqlite3")
    engine = Engine(cfg, database, None)
    job_id = engine.enqueue("scan", {}, "锁冲突后继续执行")
    engine.scan = lambda _: {}
    reached_schedule, proceed = threading.Event(), threading.Event()
    original_schedule = engine.schedule

    def schedule():
        reached_schedule.set()
        assert proceed.wait(3)
        original_schedule()

    engine.schedule = schedule
    lock = original_connect(database.path)
    try:
        engine.start()
        assert reached_schedule.wait(3)
        lock.execute("PRAGMA locking_mode=EXCLUSIVE")
        lock.execute("BEGIN EXCLUSIVE")
        lock.execute("UPDATE settings SET value=value")
        proceed.set()
        wait_for(lambda: engine.worker_failures > 0)
        assert engine.thread.is_alive()
        assert engine.health_status()["worker_state"] == "retrying"
        assert engine.worker_last_error == "database_busy"
        lock.rollback()
        lock.close()
        wait_for(lambda: database.one("SELECT state FROM jobs WHERE id=?", (job_id,))["state"] == "succeeded")
        assert engine.worker_failures == 0
        assert len(database.rows("SELECT * FROM job_runs")) == 1
    finally:
        proceed.set()
        lock.close()
        engine.close()


def test_claimed_job_is_recovered_before_next_claim(cfg):
    database = Database(cfg.data / "recover.sqlite3")
    database.set("task_policy", {"enabled": False, "interval_seconds": 0, "max_per_hour": 120})
    engine = Engine(cfg, database, None)
    job_id = engine.enqueue("scan", {}, "中断恢复")
    engine.scan = lambda _: {}
    original_run = engine.run_job
    original_recovery = engine._recover_job
    interrupted = False

    def run(job):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            clock = now()
            database.execute("UPDATE jobs SET state='running',run_started_at=? WHERE id=?", (clock, job_id))
            database.execute("INSERT INTO job_runs(job_id,started) VALUES (?,?)", (job_id, clock))
            raise sqlite3.OperationalError("database is locked")
        original_run(job)

    def recover(identifier):
        original_recovery(identifier)
        saved = database.one("SELECT * FROM jobs WHERE id=?", (identifier,))
        assert saved["state"] == "queued" and saved["next_run"] >= now() + 29
        assert saved["run_started_at"] is None and saved["timing_partial"] == 1
        database.execute("UPDATE jobs SET next_run=0 WHERE id=?", (identifier,))

    engine.run_job, engine._recover_job = run, recover
    try:
        engine.start()
        wait_for(lambda: database.one("SELECT state FROM jobs WHERE id=?", (job_id,))["state"] == "succeeded")
        assert [r["outcome"] for r in database.rows("SELECT outcome FROM job_runs ORDER BY id")] == ["interrupted", "succeeded"]
        assert engine.thread.is_alive()
    finally:
        engine.close()


def test_completed_job_is_not_requeued_on_timing_recovery(cfg):
    database = Database(cfg.data / "completed.sqlite3")
    engine = Engine(cfg, database, None)
    job_id = engine.enqueue("scan", {}, "已完成任务")
    database.execute("UPDATE jobs SET state='succeeded',run_started_at=? WHERE id=?", (now() - 2, job_id))
    database.execute("INSERT INTO job_runs(job_id,started) VALUES (?,?)", (job_id, now() - 2))
    engine._recover_job(job_id)
    first = database.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    engine._recover_job(job_id)
    second = database.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert first == second
    assert first["state"] == "succeeded" and first["elapsed_seconds"] >= 2
    assert database.one("SELECT outcome FROM job_runs")["outcome"] == "succeeded"


def test_unexpected_error_is_redacted_and_loop_recovers(cfg, caplog):
    engine = Engine(cfg, Database(cfg.data / "unexpected.sqlite3"), None)
    original = engine.schedule
    failed = False

    def schedule():
        nonlocal failed
        if not failed:
            failed = True
            raise ValueError("token=PRIVATE_CREDENTIAL")
        original()

    engine.schedule = schedule
    try:
        engine.start()
        wait_for(lambda: engine.worker_failures == 1)
        assert engine.health_status()["worker_last_error"] == "ValueError"
        assert "PRIVATE_CREDENTIAL" not in caplog.text
        wait_for(lambda: failed and engine.worker_failures == 0)
        assert engine.thread.is_alive()
    finally:
        engine.close()


def test_health_reports_worker_retry_and_recovery(cfg, monkeypatch):
    cfg.worker = True
    app = create_app(cfg)
    engine = app.state.engine
    monkeypatch.setattr(engine, "start", lambda: None)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["worker_state"] == "stopped"
        engine.thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout: None)
        engine.worker_failures = 1
        engine.worker_last_error = "database_busy"
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["worker"] is True and response.json()["worker_state"] == "retrying"
        engine.worker_failures = 0
        assert client.get("/api/health").status_code == 200


def test_health_reads_database_and_returns_quickly_when_locked(cfg, monkeypatch):
    monkeypatch.setattr(Personal, "start", lambda _: None)
    app = create_app(cfg)
    with TestClient(app) as client:
        before = client.get("/api/health")
        assert before.status_code == 200 and before.json()["worker"] is True
        assert before.json()["worker_state"] == "disabled"
        lock = sqlite3.connect(app.state.db.path)
        try:
            lock.execute("PRAGMA locking_mode=EXCLUSIVE")
            lock.execute("BEGIN EXCLUSIVE")
            lock.execute("UPDATE settings SET value=value")
            started = time.monotonic()
            response = client.get("/api/health")
            assert response.status_code == 503 and response.json()["database"] == "unavailable"
            assert time.monotonic() - started < 2
        finally:
            lock.rollback()
            lock.close()
        assert client.get("/api/health").status_code == 200
