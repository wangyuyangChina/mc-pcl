import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

import launcher


class DownloadResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class InterruptedDownloadResponse(DownloadResponse):
    def __init__(self, payload):
        super().__init__(payload)
        self.interrupted = False

    def read(self, size=-1):
        if self.interrupted:
            raise URLError("temporary TLS disconnect")
        self.interrupted = True
        return super().read(min(size, 5))


class LauncherTests(unittest.TestCase):
    def test_only_approved_https_hosts_are_downloadable(self):
        launcher._validate_url("https://piston-meta.mojang.com/manifest.json")
        launcher._validate_url("https://libraries.minecraft.net/example.jar")
        for url in (
            "http://piston-meta.mojang.com/manifest.json",
            "https://example.com/client.jar",
            "file:///tmp/client.jar",
        ):
            with self.subTest(url=url), self.assertRaises(launcher.LauncherError):
                launcher._validate_url(url)

    def test_download_verifies_sha1_and_size_before_publishing(self):
        payload = b"official client bytes"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "client.jar"
            with (
                patch.object(launcher, "_validate_url"),
                patch.object(launcher, "urlopen", return_value=DownloadResponse(payload)),
            ):
                launcher._download_file(
                    "https://piston-data.mojang.com/client.jar",
                    target,
                    hashlib.sha1(payload).hexdigest(),
                    len(payload),
                )
            self.assertEqual(target.read_bytes(), payload)

            with (
                patch.object(launcher, "_validate_url"),
                patch.object(launcher, "urlopen", return_value=DownloadResponse(payload)),
                self.assertRaises(launcher.LauncherError),
            ):
                launcher._download_file(
                    "https://piston-data.mojang.com/client.jar",
                    target,
                    "0" * 40,
                    len(payload),
                )
            self.assertEqual(target.read_bytes(), payload)

    def test_download_retries_transient_disconnect_without_double_counting_progress(self):
        payload = b"official client bytes"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "client.jar"
            task_id = launcher._new_task("install", "1.21.4")
            try:
                with (
                    patch.object(launcher, "_validate_url"),
                    patch.object(
                        launcher,
                        "urlopen",
                        side_effect=[
                            InterruptedDownloadResponse(payload),
                            DownloadResponse(payload),
                        ],
                    ),
                    patch.object(launcher.time, "sleep"),
                ):
                    launcher._download_file(
                        "https://piston-data.mojang.com/client.jar",
                        target,
                        hashlib.sha1(payload).hexdigest(),
                        len(payload),
                        task_id,
                    )
                with launcher._task_lock:
                    task = launcher._public_task(launcher._tasks[task_id])
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(task["bytes"], len(payload))
            finally:
                with launcher._task_lock:
                    launcher._tasks.pop(task_id, None)

    def test_cached_downloads_count_toward_progress(self):
        payload = b"cached verified asset"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "asset"
            target.write_bytes(payload)
            task_id = launcher._new_task("install", "1.21.4")
            try:
                launcher._download_one(
                    task_id,
                    (
                        "asset",
                        "https://resources.download.minecraft.net/asset",
                        hashlib.sha1(payload).hexdigest(),
                        len(payload),
                        target,
                    ),
                )
                with launcher._task_lock:
                    task = launcher._public_task(launcher._tasks[task_id])
                self.assertEqual(task["bytes"], len(payload))
                self.assertEqual(task["completed"], 1)
            finally:
                with launcher._task_lock:
                    launcher._tasks.pop(task_id, None)

    def test_safe_paths_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "game"
            with self.assertRaises(launcher.LauncherError):
                launcher._safe_path(base, "../outside")
            self.assertEqual(
                launcher._safe_path(base, "libraries/a.jar"),
                (base / "libraries/a.jar").resolve(),
            )

    def test_launch_command_uses_installed_version_auth_and_saved_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game_dir = root / "game"
            versions_dir = game_dir / "versions"
            libraries_dir = game_dir / "libraries"
            version_dir = versions_dir / "1.21.4"
            version_dir.mkdir(parents=True)
            library = libraries_dir / "example" / "library" / "1.0" / "library-1.0.jar"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"library")
            (version_dir / "1.21.4.jar").write_bytes(b"client")
            settings_file = root / "settings.json"
            settings_file.write_text(json.dumps({"memory": 6, "jvmArguments": "-XX:+UseG1GC"}))
            metadata = {
                "id": "1.21.4",
                "type": "release",
                "mainClass": "net.minecraft.client.main.Main",
                "javaVersion": {"majorVersion": 21},
                "assetIndex": {"id": "19"},
                "libraries": [{
                    "name": "example:library:1.0",
                    "downloads": {
                        "artifact": {
                            "path": "example/library/1.0/library-1.0.jar",
                            "url": "https://libraries.minecraft.net/example/library/1.0/library-1.0.jar",
                        }
                    },
                }],
                "arguments": {
                    "jvm": ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"],
                    "game": ["--username", "${auth_player_name}", "--accessToken", "${auth_access_token}"],
                },
            }
            auth = {
                "profile": {"id": "abc123", "name": "Player"},
                "minecraftAccessToken": "token-value",
            }
            with (
                patch.object(launcher, "GAME_DIR", game_dir),
                patch.object(launcher, "VERSIONS_DIR", versions_dir),
                patch.object(launcher, "LIBRARIES_DIR", libraries_dir),
                patch.object(launcher, "ASSETS_DIR", game_dir / "assets"),
                patch.object(launcher, "SETTINGS_FILE", settings_file),
                patch.object(launcher, "_java_info", return_value={"path": "/usr/bin/java", "major": 21}),
            ):
                command = launcher._launch_arguments(metadata, "1.21.4", auth)

            self.assertEqual(command[0], "/usr/bin/java")
            self.assertIn("-Xmx6G", command)
            self.assertIn("-XX:+UseG1GC", command)
            self.assertIn("Player", command)
            self.assertIn("token-value", command)
            self.assertEqual(command[-2], "--accessToken")
            self.assertEqual(command[-1], "token-value")

    def test_minecraft_login_uses_bearer_token_for_entitlement_and_profile(self):
        requests = []
        responses = iter((
            DownloadResponse(json.dumps({"items": [{"name": "game_minecraft"}]}).encode()),
            DownloadResponse(json.dumps({"id": "profile-id", "name": "Player"}).encode()),
        ))

        def fake_urlopen(request, timeout):
            requests.append(request)
            return next(responses)

        with (
            patch.object(
                launcher,
                "_post_json",
                side_effect=(
                    {"Token": "xbl-token", "DisplayClaims": {"xui": [{"uhs": "user-hash"}]}},
                    {"Token": "xsts-token", "DisplayClaims": {"xui": [{"uhs": "user-hash", "xid": "123"}]}},
                    {"access_token": "minecraft-token", "expires_in": 3600},
                ),
            ),
            patch.object(launcher, "urlopen", side_effect=fake_urlopen),
            patch.object(launcher, "_load_config", return_value={"microsoftClientId": "client-id"}),
            patch.object(launcher, "_write_json"),
        ):
            profile = launcher._finish_minecraft_login({
                "access_token": "microsoft-token",
                "refresh_token": "refresh-token",
            })

        self.assertEqual(profile, {"id": "profile-id", "name": "Player", "xuid": "123"})
        self.assertEqual(
            [(request.full_url, request.get_header("Authorization")) for request in requests],
            [
                (launcher.MINECRAFT_ENTITLEMENTS_URL, "Bearer minecraft-token"),
                (launcher.MINECRAFT_PROFILE_URL, "Bearer minecraft-token"),
            ],
        )

    def test_launch_rejects_java_older_than_version_requirement(self):
        metadata = {
            "javaVersion": {"majorVersion": 25},
            "downloads": {},
        }
        with patch.object(launcher, "_java_info", return_value={"path": "java", "major": 21}):
            with self.assertRaisesRegex(launcher.LauncherError, "需要 Java 25"):
                launcher._launch_arguments(metadata, "26.3", {})

    def test_catalog_uses_verified_cache_when_official_server_is_unavailable(self):
        manifest = {"versions": [{"id": "1.21.4"}]}
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "manifest.json"
            with patch.object(launcher, "_request_json", return_value=manifest):
                self.assertEqual(
                    launcher._cached_official_json("https://piston-meta.mojang.com/manifest.json", cache_path),
                    (manifest, False),
                )
            with patch.object(
                launcher,
                "_request_json",
                side_effect=launcher.LauncherError("network unavailable"),
            ):
                cached, stale = launcher._cached_official_json(
                    "https://piston-meta.mojang.com/manifest.json", cache_path
                )
            self.assertEqual(cached, manifest)
            self.assertTrue(stale)

    def test_runtime_manifest_selects_requested_java_major(self):
        runtimes = [
            {"version": {"name": "17.0.15", "released": "2025-01-01"}},
            {
                "version": {"name": "25.0.1", "released": "2025-05-01"},
                "manifest": {
                    "sha1": "a" * 40,
                    "size": 20,
                    "url": "https://piston-meta.mojang.com/java/manifest.json",
                },
            },
        ]
        index = {"linux": {"java-runtime-epsilon": runtimes}}
        files = {"bin/java": {"type": "file"}}
        with (
            patch.object(
                launcher,
                "_cached_official_json",
                side_effect=[(index, False), ({"files": files}, False)],
            ),
            patch.object(launcher, "_runtime_platform_key", return_value="linux"),
        ):
            selected, selected_files = launcher._runtime_manifest(25, "java-runtime-epsilon")
        self.assertEqual(selected["version"]["name"], "25.0.1")
        self.assertEqual(selected_files, files)

    def test_runtime_manifest_rejects_unsupported_component(self):
        with self.assertRaisesRegex(launcher.LauncherError, "运行时类型无效"):
            launcher._runtime_manifest(25, "untrusted-component")

    def test_managed_java_is_selected_when_system_java_is_too_old(self):
        managed_java = {"path": "/managed/java", "major": 25, "version": "25.0.1"}
        with (
            patch.object(launcher, "_java_info", return_value={"path": "java", "major": 21}),
            patch.object(launcher, "_installed_java", return_value=[managed_java]),
        ):
            self.assertEqual(launcher._select_java(25), managed_java)

    def test_runtime_links_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            staging = Path(temp_dir) / "runtime"
            staging.mkdir()
            with self.assertRaisesRegex(launcher.LauncherError, "越界链接"):
                launcher._extract_runtime_links(
                    {"bin/java": {"type": "link", "target": "../../outside"}},
                    staging,
                )


if __name__ == "__main__":
    unittest.main()
