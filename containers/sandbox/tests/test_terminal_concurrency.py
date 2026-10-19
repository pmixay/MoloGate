"""Run with the sandbox's Open Terminal Python to catch shared executor starvation."""

import asyncio
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from open_terminal.utils.runner import PtyRunner


@unittest.skipUnless(os.name == 'posix', 'The sandbox uses Unix PTYs')
class TerminalConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_commands_leave_workers_available_for_file_operations(self):
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        runners = [PtyRunner('sleep 30', cwd=None, env=None) for _ in range(4)]
        tasks = [
            asyncio.create_task(operation)
            for runner in runners
            for operation in (runner.read_output(None), runner.wait())
        ]
        try:
            await asyncio.sleep(0.1)
            # Skills and file tools use this same executor for filesystem access.
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(Path('/tmp').is_dir), timeout=1))
        finally:
            for runner in runners:
                runner.kill(force=True)
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            for runner in runners:
                runner.close()


if __name__ == '__main__':
    unittest.main()
