"""Tests for TaskManager class in utils/controlling.py."""

import asyncio

import pytest

from custom_components.better_thermostat.utils.controlling import TaskManager


class TestTaskManager:
    """Test TaskManager class."""

    @pytest.mark.asyncio
    async def test_create_task_adds_to_set(self):
        """Test that create_task adds task to the set."""
        manager = TaskManager()

        async def dummy_coro():
            await asyncio.sleep(0.01)
            return "done"

        task = manager.create_task(dummy_coro())

        assert task in manager.tasks
        assert len(manager.tasks) == 1

        # Wait for task to complete
        result = await task
        assert result == "done"

    @pytest.mark.asyncio
    async def test_task_removed_after_completion(self):
        """Test that task is removed from set after completion."""
        manager = TaskManager()

        async def dummy_coro():
            await asyncio.sleep(0.01)
            return "done"

        task = manager.create_task(dummy_coro())
        assert len(manager.tasks) == 1

        await task
        # Task should be removed after completion
        await asyncio.sleep(0.01)  # Give callback time to execute
        assert len(manager.tasks) == 0

    @pytest.mark.asyncio
    async def test_task_removed_after_exception(self):
        """Test that task is removed even if it raises an exception."""
        manager = TaskManager()

        async def failing_coro():
            await asyncio.sleep(0.01)
            raise ValueError("Test error")

        task = manager.create_task(failing_coro())
        assert len(manager.tasks) == 1

        with pytest.raises(ValueError, match="Test error"):
            await task

        # Task should still be removed
        await asyncio.sleep(0.01)
        assert len(manager.tasks) == 0

    @pytest.mark.asyncio
    async def test_multiple_tasks(self):
        """Test managing multiple tasks."""
        manager = TaskManager()

        async def coro(delay, value):
            await asyncio.sleep(delay)
            return value

        task1 = manager.create_task(coro(0.01, "first"))
        task2 = manager.create_task(coro(0.02, "second"))
        task3 = manager.create_task(coro(0.03, "third"))

        assert len(manager.tasks) == 3

        # Wait for all tasks
        results = await asyncio.gather(task1, task2, task3)
        assert results == ["first", "second", "third"]

        # All tasks should be removed
        await asyncio.sleep(0.01)
        assert len(manager.tasks) == 0

    @pytest.mark.asyncio
    async def test_task_cancellation_removes_from_set(self):
        """Test that cancelled tasks are removed from set."""
        manager = TaskManager()

        async def long_coro():
            await asyncio.sleep(10)
            return "done"

        task = manager.create_task(long_coro())
        assert len(manager.tasks) == 1

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Task should be removed
        await asyncio.sleep(0.01)
        assert len(manager.tasks) == 0
