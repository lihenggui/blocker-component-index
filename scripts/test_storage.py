from unittest.mock import patch
from indexlib.http import DeadlineReached
from indexlib.storage import Store, checkpointed, read_json, write_json
from test_http import Clock
from test_support import FixtureTest


class StorageTests(FixtureTest):
    def test_given_many_checkpoint_requests_when_less_than_thirty_seconds_elapse_then_json_is_written_at_most_once(self):
        clock = Clock()
        store = Store(self.root, clock=clock)
        with patch("indexlib.storage.write_json", wraps=write_json) as writes:
            for cursor in range(1000):
                store.state["cursor"] = cursor
                store.save()
            self.assertEqual(writes.call_count, 2)
            clock.now = 29.99
            store.save()
            self.assertEqual(writes.call_count, 2)
            clock.now = 30
            store.save()
            self.assertEqual(writes.call_count, 4)
        self.assertEqual(Store(self.root).state["cursor"], 999)

    def test_given_pending_changes_when_forced_checkpoint_runs_then_changes_are_saved_before_the_interval_elapses(self):
        store = Store(self.root, clock=Clock())
        store.save()
        store.state["cursor"] = 42
        store.save(force=True)
        self.assertEqual(Store(self.root).state["cursor"], 42)

    def test_given_stage_finishes_when_normal_save_is_throttled_then_final_progress_is_forced_to_disk(self):
        store = Store(self.root, clock=Clock())
        store.save()

        @checkpointed
        def stage(store):
            store.state["cursor"] = 42
            store.save()
            return False
        self.assertFalse(stage(store))
        self.assertEqual(Store(self.root).state["cursor"], 42)

    def test_given_deadline_or_error_when_stage_exits_then_unsaved_progress_is_forced_to_disk(self):
        for exception in (DeadlineReached, OSError):
            with self.subTest(exception=exception):
                store = Store(self.root, clock=Clock())
                store.save()

                @checkpointed
                def stage(store):
                    store.state["cursor"] = exception.__name__
                    store.save()
                    raise exception()
                with self.assertRaises(exception):
                    stage(store)
                self.assertEqual(Store(self.root).state["cursor"], exception.__name__)

    def test_given_existing_checkpoint_when_atomic_replace_fails_then_previous_json_remains_intact(self):
        path = self.root / "data/state/progress.json"
        write_json(path, {"cursor": 1})
        with patch("pathlib.Path.replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaises(OSError):
                write_json(path, {"cursor": 2})
        self.assertEqual(read_json(path, {}), {"cursor": 1})
