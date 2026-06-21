import threading

import pytest

from src.queue import JobQueue


def test_enqueue_dequeue():
    q = JobQueue()
    q.enqueue("job-1", "feat/a")
    q.enqueue("job-2", "feat/b")

    assert q.dequeue(timeout=0.1) == "job-1"
    q.complete("job-1")
    assert q.dequeue(timeout=0.1) == "job-2"
    q.complete("job-2")


def test_position():
    q = JobQueue()
    q.enqueue("job-1", "feat/a")
    q.enqueue("job-2", "feat/b")
    q.enqueue("job-3", "feat/c")

    assert q.position("job-1") == 0
    assert q.position("job-2") == 1
    assert q.position("job-3") == 2


def test_cancel_pending():
    q = JobQueue()
    q.enqueue("job-1", "feat/a")
    q.enqueue("job-2", "feat/b")

    assert q.cancel("job-1") is True
    assert q.dequeue(timeout=0.1) == "job-2"


def test_cancel_running():
    cancelled = []
    q = JobQueue()
    q.set_cancel_callback(lambda jid: cancelled.append(jid))
    q.enqueue("job-1", "feat/a")

    jid = q.dequeue(timeout=0.1)
    assert jid == "job-1"

    assert q.cancel("job-1") is True
    assert cancelled == ["job-1"]


def test_cancel_nonexistent():
    q = JobQueue()
    assert q.cancel("no-such") is False


def test_replace_policy_pending():
    q = JobQueue(same_branch_policy="replace")
    q.enqueue("job-1", "feat/a")
    q.enqueue("job-2", "feat/a")

    assert q.pending_count() == 1
    assert q.dequeue(timeout=0.1) == "job-2"


def test_replace_policy_running():
    cancelled = []
    q = JobQueue(same_branch_policy="replace")
    q.set_cancel_callback(lambda jid: cancelled.append(jid))

    q.enqueue("job-1", "feat/a")
    q.dequeue(timeout=0.1)

    q.enqueue("job-2", "feat/a")
    assert cancelled == ["job-1"]
    assert q.dequeue(timeout=0.1) == "job-2"


def test_queue_policy():
    q = JobQueue(same_branch_policy="queue")
    q.enqueue("job-1", "feat/a")
    q.enqueue("job-2", "feat/a")

    assert q.pending_count() == 2
    assert q.dequeue(timeout=0.1) == "job-1"
    q.complete("job-1")
    assert q.dequeue(timeout=0.1) == "job-2"


def test_reject_policy():
    q = JobQueue(same_branch_policy="reject")
    pos = q.enqueue("job-1", "feat/a")
    assert pos == 0

    pos = q.enqueue("job-2", "feat/a")
    assert pos is None
    assert q.pending_count() == 1


def test_reject_policy_different_branch():
    q = JobQueue(same_branch_policy="reject")
    q.enqueue("job-1", "feat/a")
    pos = q.enqueue("job-2", "feat/b")
    assert pos is not None
    assert q.pending_count() == 2


def test_dequeue_blocks():
    q = JobQueue()
    result = []

    def producer():
        import time
        time.sleep(0.05)
        q.enqueue("job-1", "feat/a")

    t = threading.Thread(target=producer)
    t.start()

    jid = q.dequeue(timeout=1.0)
    assert jid == "job-1"
    t.join()
