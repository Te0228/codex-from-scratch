"""Controlled Python call plans using the same cell lifecycle as the notebook.

This deliberately does not evaluate Python or JavaScript supplied by a model.
Only host-selected dispatch functions can execute, with one captured deadline.
"""
import asyncio
from .scheduler_probe import Cell


class PlanRuntime:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.ledger = []
        self.gate = asyncio.Semaphore(3)

    async def invoke(self, name, arguments, *, origin, cell=None):
        await cell.ready.wait()
        if cell.stopping:
            raise asyncio.CancelledError
        async with self.gate:
            result = await asyncio.to_thread(self.dispatch, name, arguments)
            self.ledger.append({'name':name,'outcome':result['outcome']})
            return result


async def execute_plan(calls, dispatch, session_store, cell_id):
    runtime = PlanRuntime(dispatch)
    cell = Cell(cell_id, runtime, session_store)
    tasks = [cell.start_call(call['name'],call.get('arguments',{})) for call in calls]
    cell.ready.set()
    try:
        results = await asyncio.gather(*tasks)
        cell.store('last_results', results)
        await cell.close()
        return {'runtime':'python-controlled-call-plan','results':results,'ledger':runtime.ledger}
    except BaseException:
        await cell.close(terminate=True)
        raise


def complete(coroutine):
    """Run a synchronous service call from CLI, worker thread or notebook loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run,coroutine).result()
