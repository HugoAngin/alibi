from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import pytest

from agents.eval import parallel_capture as scheduler


def test_parallel_completion_preserves_order_and_each_outcome():
    started = [Event() for _ in range(4)]
    release = Event()
    lock = Lock()
    state = {"active": 0, "maximum": 0}
    observed = []

    def job(index):
        with lock:
            state['active'] += 1
            state['maximum'] = max(state['maximum'], state['active'])
        if index < 4:
            started[index].set()
            if index == 3:
                assert all(event.wait(1) for event in started)
                release.set()
            assert release.wait(1)
        with lock:
            state['active'] -= 1
        if index == 5:
            raise ValueError('sensitive exception text must not be retained')
        return index * 2

    result = scheduler.schedule(range(9), job, progress=lambda row, meta: observed.append((row, meta)))
    assert state['maximum'] == 4
    assert [row['job'] for row in result['outcomes']] == list(range(9))
    assert result['outcomes'][5]['status'] == 'error'
    assert result['outcomes'][5]['error'] == 'ValueError'
    assert 'sensitive' not in str(result)
    assert len(observed) == 9
    assert len({row['index'] for row, _ in observed}) == 9
    assert result['metadata']['attempted'] == 9
    assert result['metadata']['completed'] == 8
    assert result['metadata']['errors'] == 1


def test_expired_deadline_keeps_all_jobs_as_not_started():
    called = []
    result = scheduler.schedule(range(5), lambda job: pytest.fail('must not start'),
        deadline=datetime.now(timezone.utc)-timedelta(seconds=1),
        progress=lambda row, _meta: called.append(row))
    assert result['metadata']['attempted'] == 0
    assert result['metadata']['not_started'] == 5
    assert result['metadata']['deadline_reached']
    assert len(called) == 5
    assert all(row['reason'] == 'deadline' for row in result['outcomes'])


def test_deadline_during_run_drains_active_and_skips_queue(monkeypatch):
    base = datetime(2026, 11, 8, 11, 49, tzinfo=timezone.utc)
    state = {'time': base}
    monkeypatch.setattr(scheduler, '_now', lambda: state['time'])

    def run(job):
        state['time'] = base + timedelta(minutes=2)
        return job

    result = scheduler.schedule(range(5), run, workers=1, deadline=base+timedelta(minutes=1))
    assert result['metadata']['attempted'] == 1
    assert result['metadata']['completed'] == 1
    assert result['metadata']['not_started'] == 4


def test_deadline_reached_while_final_job_is_running(monkeypatch):
    base = datetime(2026, 11, 8, 11, 49, tzinfo=timezone.utc)
    state = {'time': base}
    monkeypatch.setattr(scheduler, '_now', lambda: state['time'])

    def run(job):
        state['time'] = base + timedelta(minutes=2)
        return job

    result = scheduler.schedule([0], run, workers=1, deadline=base+timedelta(minutes=1))
    assert result['metadata']['completed'] == 1
    assert result['metadata']['deadline_reached']


def test_rate_limit_reduces_to_two_without_rerunning_jobs():
    events = [Event() for _ in range(4)]
    release_initial = Event()
    lock = Lock()
    seen = []
    after = {'active': 0, 'maximum': 0}
    next_pair = Event()

    def progress(outcome, meta):
        if outcome['index'] == 0:
            assert meta['final_workers'] == 2
            release_initial.set()

    def run(index):
        with lock:
            seen.append(index)
        if index < 4:
            events[index].set()
            if index == 0:
                assert all(event.wait(1) for event in events)
                return [], {'rate_limited': True}
            assert release_initial.wait(1)
            return [], {'rate_limited': False}
        with lock:
            after['active'] += 1
            after['maximum'] = max(after['maximum'], after['active'])
            if after['active'] == 2:
                next_pair.set()
        assert next_pair.wait(1)
        with lock:
            after['active'] -= 1
        return [], {'rate_limited': False}

    result = scheduler.schedule(range(12), run, workers=4, progress=progress)
    assert sorted(seen) == list(range(12))
    assert len(seen) == 12
    assert after['maximum'] == 2
    assert result['metadata']['rate_limit_reduced']
    assert result['metadata']['final_workers'] == 2
    assert result['metadata']['errors'] == 0


@pytest.mark.parametrize('workers', [0, 5, 1.5, True])
def test_rejects_invalid_worker_counts(workers):
    with pytest.raises(ValueError, match='workers'):
        scheduler.schedule([], lambda job: job, workers=workers)


def test_rejects_naive_deadline_and_handles_empty_input():
    with pytest.raises(ValueError, match='timezone-aware'):
        scheduler.schedule([], lambda job: job, deadline=datetime.now())
    assert scheduler.schedule([], lambda job: job)['metadata']['total'] == 0


def test_infrastructure_error_stops_launches_and_drains_in_flight():
    started = [Event() for _ in range(4)]
    release = Event()
    seen = []
    observed = []

    def run(index):
        seen.append(index)
        assert index < 4, "queued job started after infrastructure failure"
        started[index].set()
        if index == 0:
            assert all(event.wait(1) for event in started)
            raise RuntimeError("sensitive infrastructure detail")
        assert release.wait(1)
        return index

    def progress(row, meta):
        observed.append(row)
        if row['status'] == 'error':
            assert meta['aborted']
            assert meta['abort_reason'] == 'RuntimeError'
            release.set()

    result = scheduler.schedule(range(10), run, workers=4, stop_on_error=True, progress=progress)
    assert sorted(seen) == list(range(4))
    assert len(observed) == 10
    assert len({row['index'] for row in observed}) == 10
    assert result['metadata']['attempted'] == 4
    assert result['metadata']['completed'] == 3
    assert result['metadata']['errors'] == 1
    assert result['metadata']['not_started'] == 6
    assert all(row['reason'] == 'capture_integrity' for row in result['outcomes'][4:])
    assert 'sensitive' not in str(result)


def test_adapter_failure_result_does_not_abort():
    seen = []

    def run(index):
        seen.append(index)
        return [{'failure': {'type': 'timeout'}}], {}

    result = scheduler.schedule(range(6), run, workers=1, stop_on_error=True)
    assert seen == list(range(6))
    assert not result['metadata']['aborted']
    assert result['metadata']['abort_reason'] is None
    assert result['metadata']['completed'] == 6


def test_heartbeat_runs_without_completion_and_bounds_wait(monkeypatch):
    release = Event()
    real_wait = scheduler.wait
    waits = []
    beats = []

    def wait(futures, *, timeout, return_when):
        waits.append(timeout)
        if len(waits) == 1:
            return set(), set(futures)
        return real_wait(futures, timeout=timeout, return_when=return_when)

    def heartbeat(meta):
        beats.append(meta)
        release.set()

    def run(index):
        assert release.wait(1)
        return index

    monkeypatch.setattr(scheduler, "wait", wait)
    result = scheduler.schedule(range(4), run, heartbeat=heartbeat)
    assert beats[0]['attempted'] == 4 and beats[0]['completed'] == 0
    assert all(timeout <= 1 for timeout in waits)
    assert result['metadata']['completed'] == 4


def test_deadline_during_setup_is_not_an_integrity_abort(monkeypatch):
    now = datetime.now(timezone.utc)
    current = [now]
    deadline = now + timedelta(seconds=1)
    monkeypatch.setattr(scheduler, '_now', lambda: current[0])

    def run(_job):
        current[0] = deadline + timedelta(seconds=1)
        raise TimeoutError('deadline reached during setup')

    result = scheduler.schedule(range(3), run, workers=1, deadline=deadline, stop_on_error=True)
    assert result['metadata']['deadline_reached']
    assert not result['metadata']['aborted']
    assert result['metadata']['errors'] == 1
    assert result['metadata']['not_started'] == 2
    assert all(row['reason'] == 'deadline' for row in result['outcomes'][1:])
