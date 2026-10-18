"""Desktop config contracts; runnable with pytest or stdlib unittest."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src.config import config_repository as repository
from src.config.bootstrap import DEFAULT_BOOTSTRAP, load_bootstrap
from src.config.redis_store import ConfigUnavailable, RevisionConflict
from src.config.schema import BUSINESS_DEFAULTS, default_document
from src.config.sqlite_config import SQLiteConfigProvider, desktop_default_document


class DesktopConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "Application Support" / "TranslateSub"
        self.path = self.root / "config.db"
        env = patch.dict(os.environ, {
            "SUBTRANS_DESKTOP": "1", "SUBTRANS_DESKTOP_HOME": str(self.home),
            "SUBTRANS_DESKTOP_FFMPEG": "", "SUBTRANS_DESKTOP_FFPROBE": "",
        })
        env.start()
        self.addCleanup(env.stop)
        original_provider = repository._provider
        self.addCleanup(repository.set_config_provider, original_provider)
        repository.set_config_provider(None)

    def test_desktop_paths_ignore_server_bootstrap(self):
        server = self.root / "server.json"
        server.write_text("this must never be read")
        self.assertEqual(load_bootstrap(server)["paths"], {
            "dataDir": str(self.home / "data"),
            "dbPath": str(self.home / "db" / "app.db"),
            "configDbPath": str(self.home / "db" / "config.db"),
        })
        from src.config.config import Settings
        self.assertEqual(Settings().data_dir, self.home / "data")
        self.assertEqual(Settings().db_path, self.home / "db" / "app.db")
        with patch.dict(os.environ, {"SUBTRANS_DESKTOP_HOME": "relative"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                load_bootstrap()

    def test_desktop_home_default(self):
        with patch.dict(os.environ, {"SUBTRANS_DESKTOP_HOME": ""}):
            expected = Path.home() / "Library" / "Application Support" / "TranslateSub"
            self.assertEqual(load_bootstrap()["paths"]["dataDir"], str(expected.resolve() / "data"))

    def test_factory_defaults_and_subscriptions(self):
        with patch.dict(os.environ, {
            "SUBTRANS_DESKTOP_FFMPEG": str(self.root / "Bundle" / "ffmpeg"),
            "SUBTRANS_DESKTOP_FFPROBE": str(self.root / "Bundle" / "ffprobe"),
            "SUBTRANS_DESKTOP_SESSION_TOKEN": "test-session-secret",
        }):
            provider = repository.get_config_provider()
            self.assertIsInstance(provider, SQLiteConfigProvider)
            self.assertIs(repository.get_config_provider(), provider)
            self.assertFalse(provider.db_path.exists())
            snapshot = repository.initialize_config_document()
        business = snapshot.values["business"]
        expected = {
            "pipeline_workers": 1, "startup_probe_enabled": False,
            "local_whisper_model": "base.en", "local_whisper_device": "cpu",
            "local_whisper_compute_type": "int8",
            "ffmpeg_bin": str(self.root / "Bundle" / "ffmpeg"),
            "ffprobe_bin": str(self.root / "Bundle" / "ffprobe"),
        }
        for key, value in expected.items():
            self.assertEqual(business[key], value)
        self.assertEqual(snapshot.values["models"]["whisper"]["model"], "local:base.en")
        self.assertFalse(snapshot.values["models"]["llm"]["enabled"])
        self.assertFalse(snapshot.values["models"]["tts"]["enabled"])
        self.assertNotIn(b"test-session-secret", provider.db_path.read_bytes())
        self.assertEqual(provider.db_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(repository.start_config_subscription())
        repository.stop_config_subscription()
        self.assertEqual(BUSINESS_DEFAULTS["pipeline_workers"], 8)
        self.assertTrue(BUSINESS_DEFAULTS["startup_probe_enabled"])

    def test_server_factory_stays_redis(self):
        server = self.root / "server.json"
        server.write_text(json.dumps({"paths": {"dataDir": "server-data"}}))
        for flag in (None, "0", "true", "yes"):
            with self.subTest(flag=flag), patch.dict(os.environ):
                if flag is None:
                    os.environ.pop("SUBTRANS_DESKTOP", None)
                else:
                    os.environ["SUBTRANS_DESKTOP"] = flag
                bootstrap = load_bootstrap(server)
                self.assertEqual(bootstrap["paths"]["dataDir"], "server-data")
                self.assertNotIn("configDbPath", bootstrap["paths"])
                with patch.object(repository, "load_bootstrap", return_value=copy.deepcopy(DEFAULT_BOOTSTRAP)):
                    with patch.object(repository, "RedisJsonConfigStore") as store:
                        repository.set_config_provider(None)
                        self.assertIsInstance(repository.get_config_provider(), repository.RedisConfigProvider)
                        self.assertEqual(store.call_args.kwargs["url"], DEFAULT_BOOTSTRAP["redis"]["url"])
        self.assertFalse(self.home.exists())

    def test_relative_tool_paths_rejected(self):
        for variable in ("SUBTRANS_DESKTOP_FFMPEG", "SUBTRANS_DESKTOP_FFPROBE"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: "relative/tool"}):
                with self.assertRaisesRegex(ValueError, "absolute"):
                    desktop_default_document()

    def test_restart_persistence_and_idempotent_initialization(self):
        provider = SQLiteConfigProvider(self.path)
        with self.assertRaises(ConfigUnavailable):
            provider.read()
        with self.assertRaises(ConfigUnavailable):
            provider.mutate(lambda doc: None)
        initial = provider.initialize()
        changed = provider.mutate(lambda doc: doc["business"].update(pipeline_workers=3))
        self.assertEqual(changed.revision, initial.revision + 1)
        self.assertEqual(changed.updated_by, "api")
        restarted = SQLiteConfigProvider(self.path, initial_document=default_document())
        self.assertEqual(restarted.initialize(), changed)
        self.assertEqual(restarted.read(), changed)
        changed.values["business"]["pipeline_workers"] = 999
        self.assertEqual(restarted.read().values["business"]["pipeline_workers"], 3)
        code = (
            "from src.config.sqlite_config import SQLiteConfigProvider; import sys; "
            "p=SQLiteConfigProvider(sys.argv[1]); "
            "assert p.initialize().values['business']['pipeline_workers']==3; "
            "assert p.read().revision==2"
        )
        subprocess.run([sys.executable, "-c", code, str(self.path)], check=True, timeout=15)

    def test_concurrent_initializers_and_unconditional_writes(self):
        barrier = threading.Barrier(8)
        def initialize(_):
            provider = SQLiteConfigProvider(self.path)
            barrier.wait(timeout=5)
            return provider.initialize()
        with ThreadPoolExecutor(max_workers=8) as executor:
            snapshots = list(executor.map(initialize, range(8)))
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))
        self.assertEqual(snapshots[0].revision, 1)
        def increment(_):
            return SQLiteConfigProvider(self.path).mutate(lambda doc: doc["business"].update(
                pipeline_workers=doc["business"]["pipeline_workers"] + 1
            )).revision
        with ThreadPoolExecutor(max_workers=8) as executor:
            revisions = list(executor.map(increment, range(32)))
        self.assertEqual(sorted(revisions), list(range(2, 34)))
        self.assertEqual(SQLiteConfigProvider(self.path).read().values["business"]["pipeline_workers"], 33)

    def test_bundle_relocation_refreshes_settings_without_resetting_config(self):
        from src.config.config import Settings

        for restart in (False, True):
            with self.subTest(restart=restart):
                path = self.root / f"relocation-{restart}.db"
                paths_a = {
                    "SUBTRANS_DESKTOP_FFMPEG": str(self.root / "App A" / "ffmpeg"),
                    "SUBTRANS_DESKTOP_FFPROBE": str(self.root / "App A" / "ffprobe"),
                }
                paths_b = {key: value.replace("App A", "App B") for key, value in paths_a.items()}
                with patch.dict(os.environ, paths_a):
                    provider = SQLiteConfigProvider(path)
                    repository.set_config_provider(provider)
                    initial = repository.initialize_config_document()
                    self.assertEqual(initial.revision, 1)
                    settings = Settings()
                    self.assertEqual(settings.ffmpeg_bin, paths_a["SUBTRANS_DESKTOP_FFMPEG"])

                    def customize(doc):
                        doc["business"].update(pipeline_workers=3, download_quality="720p")
                        doc["models"]["whisper"].update(language="fr", threads=2)
                        doc["engines"]["byId"]["local"] = {
                            "id": "local", "name": "My local engine", "base_url": "",
                            "api_type": "local_ct2", "model": "custom-local-model", "api_key": None,
                        }
                        doc["meta"]["legacy_sources_imported"] = ["retained-source"]

                    saved = provider.mutate(customize)
                    self.assertEqual(provider.initialize(), saved)
                with patch.dict(os.environ, {
                    **paths_b, "SUBTRANS_DESKTOP_SESSION_TOKEN": "relocation-session-secret",
                }):
                    if restart:
                        provider = SQLiteConfigProvider(path)
                        repository.set_config_provider(provider)
                    refreshed = repository.initialize_config_document()
                    expected = copy.deepcopy(saved.values)
                    expected["business"].update(
                        ffmpeg_bin=paths_b["SUBTRANS_DESKTOP_FFMPEG"],
                        ffprobe_bin=paths_b["SUBTRANS_DESKTOP_FFPROBE"],
                    )
                    self.assertEqual(refreshed.values, expected)
                    self.assertEqual(refreshed.revision, saved.revision + 1)
                    self.assertEqual(refreshed.updated_by, "bootstrap")
                    self.assertEqual(settings.ffmpeg_bin, paths_b["SUBTRANS_DESKTOP_FFMPEG"])
                    self.assertEqual(settings.ffprobe_bin, paths_b["SUBTRANS_DESKTOP_FFPROBE"])
                    self.assertEqual(settings.capture_business_snapshot()["ffmpeg_bin"],
                                     paths_b["SUBTRANS_DESKTOP_FFMPEG"])
                    self.assertEqual(provider.initialize(), refreshed)
                    self.assertEqual(SQLiteConfigProvider(path).initialize(), refreshed)
                    with self.assertRaises(RevisionConflict):
                        provider.mutate(lambda doc: self.fail("stale callback ran"),
                                        expected_revision=saved.revision)
                    self.assertNotIn(b"relocation-session-secret", path.read_bytes())
                # Missing launcher values must not replace saved tools with defaults.
                self.assertEqual(provider.initialize(), refreshed)

    def test_invalid_relocation_paths_do_not_partially_refresh(self):
        provider = SQLiteConfigProvider(self.path)
        before = provider.initialize()
        with patch.dict(os.environ, {
            "SUBTRANS_DESKTOP_FFMPEG": str(self.root / "App B" / "ffmpeg"),
            "SUBTRANS_DESKTOP_FFPROBE": "relative/ffprobe",
        }):
            with self.assertRaisesRegex(ValueError, "absolute"):
                provider.initialize()
        self.assertEqual(provider.read(), before)

    def test_concurrent_relocation_initializers_increment_revision_once(self):
        provider = SQLiteConfigProvider(self.path)
        before = provider.initialize()
        barrier = threading.Barrier(8)

        def initialize(_):
            other = SQLiteConfigProvider(self.path)
            barrier.wait(timeout=5)
            return other.initialize()

        with patch.dict(os.environ, {
            "SUBTRANS_DESKTOP_FFMPEG": str(self.root / "App B" / "ffmpeg"),
            "SUBTRANS_DESKTOP_FFPROBE": str(self.root / "App B" / "ffprobe"),
        }):
            with ThreadPoolExecutor(max_workers=8) as executor:
                snapshots = list(executor.map(initialize, range(8)))
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))
        self.assertEqual(snapshots[0].revision, before.revision + 1)

    def test_cross_process_writes_are_serialized(self):
        provider = SQLiteConfigProvider(self.path)
        provider.initialize()
        code = """
import sys
from src.config.sqlite_config import SQLiteConfigProvider
p = SQLiteConfigProvider(sys.argv[1])
for _ in range(10):
    p.mutate(lambda d: d['business'].update(pipeline_workers=d['business']['pipeline_workers']+1))
"""
        children = [subprocess.Popen([sys.executable, "-c", code, str(self.path)]) for _ in range(4)]
        try:
            self.assertEqual([child.wait(timeout=20) for child in children], [0] * 4)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        self.assertEqual(provider.read().revision, 41)
        self.assertEqual(provider.read().values["business"]["pipeline_workers"], 41)

    def test_concurrent_cas_has_one_winner_and_skips_stale_callback(self):
        provider = SQLiteConfigProvider(self.path)
        initial = provider.initialize()
        barrier = threading.Barrier(2)
        callbacks = []
        def update(worker_count):
            other = SQLiteConfigProvider(self.path)
            barrier.wait(timeout=5)
            def callback(doc):
                callbacks.append(worker_count)
                doc["business"]["pipeline_workers"] = worker_count
            try:
                return other.mutate(callback, expected_revision=initial.revision)
            except RevisionConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, [3, 4]))
        self.assertEqual(len([result for result in results if result]), 1)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(provider.read().revision, 2)
        self.assertEqual(provider.read().values["business"]["pipeline_workers"], callbacks[0])

    def test_validation_parity_and_atomic_rollback(self):
        provider = SQLiteConfigProvider(self.path)
        before = provider.initialize()
        patches = [
            {"business": {"pipeline_workers": 0}},
            {"business": {"pipeline_workers": True}},
            {"business": {"download_workers": None}},
            {"business": {"not_a_setting": 1}},
            {"models": {"whisper": {"enabled": "yes"}}},
            {"models": {"llm": {"models": [{"id": "same"}, {"id": "same"}]}}},
            {"engines": {"byId": {"bad": {"id": "different"}}}},
            {"meta": {"legacy_sources_imported": "bad"}},
        ]
        for update in patches:
            with self.subTest(update=update):
                candidate = copy.deepcopy(before.values)
                candidate.update(update)
                with self.assertRaises(ValueError):
                    repository._validate_document(candidate)
                with self.assertRaises(ValueError):
                    provider.mutate(lambda doc: candidate)
                self.assertEqual(provider.read(), before)

    def test_credentials_never_reach_database(self):
        provider = SQLiteConfigProvider(self.path)
        before = provider.initialize()
        patches = [
            {"business": {"api_token": "test-secret"}},
            {"business": {"transcriber_api_key": "test-secret"}},
            {"models": {"llm": {"apiKey": "test-secret"}}},
            {"models": {"llm": {"models": [{"id": "cloud", "api_key": "test-secret"}]}}},
            {"meta": {"SUBTRANS_DESKTOP_SESSION_TOKEN": "test-secret"}},
            {"models": {"llm": {"baseUrl": "https://user:test-secret@example.com"}}},
            {"models": {"llm": {"baseUrl": "https://example.com/?api_key=test-secret"}}},
        ]
        for index, update in enumerate(patches):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError, "cannot persist") as error:
                    provider.mutate(lambda doc: doc.update(update))
                self.assertNotIn("test-secret", str(error.exception))
                self.assertEqual(provider.read(), before)
                self.assertNotIn(b"test-secret", self.path.read_bytes())
                invalid = copy.deepcopy(before.values)
                invalid.update(update)
                with self.assertRaisesRegex(ValueError, "cannot persist"):
                    SQLiteConfigProvider(self.root / f"invalid-{index}.db", initial_document=invalid).initialize()

    def test_callback_failure_and_non_json_values_roll_back(self):
        provider = SQLiteConfigProvider(self.path)
        before = provider.initialize()
        def failing_callback(doc):
            doc["business"]["pipeline_workers"] = 77
            raise RuntimeError("callback failed")
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            provider.mutate(failing_callback)
        with self.assertRaises(TypeError):
            provider.mutate(lambda doc: doc["meta"].update(non_json=object()))
        self.assertEqual(provider.read(), before)

    def test_damaged_or_missing_documents_never_reset(self):
        statements = [
            "DELETE FROM runtime_config", "UPDATE runtime_config SET document='{}'",
            "UPDATE runtime_config SET document='not-json'", "PRAGMA user_version=999",
        ]
        for index, statement in enumerate(statements):
            with self.subTest(statement=statement):
                provider = SQLiteConfigProvider(self.root / f"damage-{index}.db")
                provider.initialize()
                with sqlite3.connect(provider.db_path) as connection:
                    connection.execute(statement)
                for operation in (provider.read, provider.initialize):
                    with self.assertRaises(ConfigUnavailable):
                        operation()

    def test_busy_database_fails_without_partial_write(self):
        provider = SQLiteConfigProvider(self.path, timeout=0.01)
        before = provider.initialize()
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ConfigUnavailable, "unavailable"):
                provider.mutate(lambda doc: doc["business"].update(pipeline_workers=7))
        self.assertEqual(provider.read(), before)

    def test_aggregate_replacement_and_store_integration(self):
        from src.store.model_settings_store import ModelSettingsStore
        from src.store.translation_engine_store import TranslationEngineStore
        provider = SQLiteConfigProvider(self.path)
        initial = provider.initialize()
        candidate = copy.deepcopy(initial.values)
        candidate["models"]["whisper"]["language"] = "en"
        candidate["engines"]["byId"]["local"] = {
            "id": "local", "name": "Local", "base_url": "", "api_type": "local_ct2",
            "model": "Helsinki-NLP/opus-mt-en-zh", "api_key": None,
        }
        result = provider.mutate(lambda _: candidate, expected_revision=1)
        candidate["engines"]["byId"].clear()
        self.assertEqual(result.revision, 2)
        self.assertEqual(ModelSettingsStore(provider=provider).get("whisper")["language"], "en")
        self.assertEqual(TranslationEngineStore(provider=provider).get("local").api_type, "local_ct2")

    def test_reader_sees_committed_snapshot_and_crash_rolls_back(self):
        provider = SQLiteConfigProvider(self.path)
        before = provider.initialize()
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE runtime_config SET revision=2")
            self.assertEqual(provider.read(), before)
            connection.rollback()
        code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE runtime_config SET revision=2')
os._exit(9)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.path)], timeout=15)
        self.assertEqual(child.returncode, 9)
        self.assertEqual(SQLiteConfigProvider(self.path).initialize(), before)


if __name__ == "__main__":
    unittest.main()
