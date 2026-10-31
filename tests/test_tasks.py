"""有界任务执行器（PRD §6.1 过载护栏）的行为测试。"""
import threading
import time

import pytest

from pr_reviewer.tasks import QueueFull, TaskRunner


def wait_until(predicate, timeout=3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_pending_returns_to_zero_after_jobs_finish():
    runner = TaskRunner(max_workers=2, max_pending=5)
    done = threading.Event()
    runner.submit(done.set)
    assert done.wait(3)
    assert wait_until(lambda: runner.pending == 0)
    runner.shutdown()


def test_over_limit_submission_is_rejected_and_leaves_no_state():
    """被拒绝的提交不该占住名额，也不该留下副作用。"""
    runner = TaskRunner(max_workers=1, max_pending=1)
    gate, started = threading.Event(), threading.Event()
    runner.submit(lambda: (started.set(), gate.wait(5)))
    assert started.wait(3)

    with pytest.raises(QueueFull, match="稍后重试"):
        runner.submit(lambda: None)
    assert runner.pending == 1
    assert runner.stats["rejected_total"] == 1

    gate.set()
    assert wait_until(lambda: runner.pending == 0)
    runner.shutdown()


def test_workers_are_bounded():
    """并发上限就是"最多几个 PR 同时在烧 LLM"，不能靠默认线程池的 40。"""
    runner = TaskRunner(max_workers=2, max_pending=10)
    running = threading.Semaphore(2)
    peak = []

    def job():
        assert running.acquire(timeout=3), "超出并发上限"
        peak.append(1)
        time.sleep(0.05)
        running.release()

    for _ in range(8):
        runner.submit(job)
    assert wait_until(lambda: runner.pending == 0, timeout=10)
    assert len(peak) == 8
    runner.shutdown()


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        TaskRunner(max_workers=0)
    with pytest.raises(ValueError):
        TaskRunner(max_pending=0)
