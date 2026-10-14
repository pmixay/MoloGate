"""Task limits: defaults, hard ranges, and "0 = off" where a limit allows it."""

import unittest
import unittest.mock

from open_webui.utils import task_limits


class ClampTest(unittest.TestCase):
    def test_values_stay_in_range(self):
        self.assertEqual(task_limits.clamp('subagents_per_chat', 50), 10)
        self.assertEqual(task_limits.clamp('subagents_per_chat', -3), 1)
        self.assertEqual(task_limits.clamp('monitor_interval_seconds', 5), 15)

    def test_zero_means_off_only_where_allowed(self):
        self.assertEqual(task_limits.clamp('auto_background_seconds', 0), 0)
        self.assertEqual(task_limits.clamp('schedule_low_limit_percent', 0), 0)
        self.assertEqual(task_limits.clamp('schedule_max_failures', 0), 1)

    def test_garbage_falls_back_to_the_default(self):
        self.assertEqual(task_limits.clamp('monitors_per_user', 'lots'), 5)
        self.assertEqual(task_limits.clamp('monitors_per_user', None), 5)

    def test_every_default_is_within_its_range(self):
        for name, limit in task_limits.LIMITS.items():
            self.assertEqual(task_limits.clamp(name, limit.default), limit.default, name)


class StoredValuesTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_stored_value_cannot_lift_a_cap(self):
        with unittest.mock.patch.object(task_limits.Config, 'get', return_value=999):
            self.assertEqual(await task_limits.get('background_tasks_per_user'), 15)

    async def test_saving_clamps_and_ignores_unknown_names(self):
        upsert = unittest.mock.AsyncMock()
        with (
            unittest.mock.patch.object(task_limits.Config, 'upsert', upsert),
            unittest.mock.patch.object(task_limits.Config, 'get_many', return_value={}),
        ):
            await task_limits.set_many({'schedule_interval_seconds': 60, 'not_a_limit': 3})

        upsert.assert_awaited_once_with({'tasks.limits.schedule_interval_seconds': 900})


if __name__ == '__main__':
    unittest.main()
