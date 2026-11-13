"""Synchronous adapter over the same chapter Scheduler, including real finalization."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from .scheduler import Scheduler


class CleanupReport:
    def __init__(self, cleanup):
        self.cleanup = cleanup

    async def close_and_join(self, owner, cancel):
        return {'cleanup': self.cleanup}


def complete(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Notebook hosts already run an event loop. Do not nest asyncio.run in it.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='mini-codex-finalize') as pool:
        return pool.submit(asyncio.run, coroutine).result()


class SerialRunOwner:
    def __init__(self):
        self.scheduler = Scheduler('not-started')
        self.lock = RLock()

    def start(self, run_id, prompt):
        with self.lock:
            if self.scheduler.active is not None:
                raise RuntimeError('run_slot_busy_or_cleanup_unknown')
            self.scheduler.run_id = run_id
            status, ref = self.scheduler.start_if_idle(run_id + ':input', prompt)
            if status != 'started':
                raise RuntimeError(status)
            consumed = self.scheduler.claim(ref)
            assert len(consumed) == 1 and consumed[0].text == prompt
            return ref

    def finish(self, ref, *, cancelled, cleanup):
        with self.lock:
            self.scheduler.attach_participant(ref, CleanupReport(cleanup))
            if cancelled:
                complete(self.scheduler.interrupt(ref))
            else:
                if not self.scheduler.begin_finish(ref):
                    raise RuntimeError('unconsumed_input_at_finish')
                complete(self.scheduler.finish(ref))
            return self.scheduler.terminal[ref]
