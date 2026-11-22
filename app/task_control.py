"""持久任务限流与计时，不影响前台音频读取。"""

DEFAULT_POLICY = {"enabled": True, "interval_seconds": 10, "max_per_hour": 120}


def policy(db):
    return {**DEFAULT_POLICY, **db.get("task_policy", {})}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise ValueError("请填写完整的任务限流设置")
    if type(value["enabled"]) is not bool:
        raise ValueError("限流开关必须为布尔值")
    for key, low, high in [("interval_seconds", 0, 3600), ("max_per_hour", 1, 3600)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("处理间隔应为 0–3600 秒，每小时上限应为 1–3600 次")
    return value


def rate_status(db, at):
    config = policy(db)
    runs = db.rows("SELECT started FROM job_runs WHERE started>? ORDER BY started", (at - 3600,))
    last = db.one("SELECT max(started) t FROM job_runs")["t"]
    until = at
    reasons = []
    if config["enabled"]:
        if last is not None and last + config["interval_seconds"] > at:
            until = last + config["interval_seconds"]
            reasons.append("任务启动间隔")
        if len(runs) >= config["max_per_hour"]:
            until = max(until, runs[-config["max_per_hour"]]["started"] + 3600)
            reasons.append("每小时启动上限")
    return {
        **config,
        "starts_last_hour": len(runs),
        "wait_seconds": max(0, until - at),
        "next_allowed_at": until,
        "reason": "、".join(reasons),
        "concurrency": 1,
    }


def timing(job, at):
    elapsed = job["elapsed_seconds"] or 0
    if job["state"] == "running" and job["run_started_at"] is not None:
        elapsed += max(0, at - job["run_started_at"])
    return {"elapsed_seconds": elapsed if job["started_at"] is not None else None, "timing_partial": bool(job["timing_partial"])}
