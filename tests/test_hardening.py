"""Offline regression tests; no Docker, network or VPS required."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import cascade_lib as lib


class TemplateGuardTests(unittest.TestCase):
    def setUp(self):
        self.before = {"routing": {"rules": []}, "outbounds": [{"tag": "direct"}]}
        self.plan = copy.deepcopy(self.before)
        self.plan["outbounds"].append({"tag": "to-foreign"})

    def test_missing_planned_change_rejected(self):
        ok, missing, _ = lib.assert_changes_planned(self.before, self.before, self.plan)
        self.assertFalse(ok)
        self.assertTrue(missing)

    def test_exact_plan_accepted(self):
        ok, _, _ = lib.assert_changes_planned(self.before, self.plan, self.plan)
        self.assertTrue(ok)

    def test_unplanned_change_rejected(self):
        after = copy.deepcopy(self.plan)
        after["dns"] = {"servers": ["9.9.9.9"]}
        ok, _, _ = lib.assert_changes_planned(self.before, after, self.plan)
        self.assertFalse(ok)

    def test_failed_rollback_not_reported_as_success(self):
        p = lib.Panel("http://localhost", "mock")
        p.get_saved_template = lambda: copy.deepcopy(self.before)
        p.update_template = lambda _: {"success": True}
        p.restore_template = lambda _: False
        with patch.object(lib.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "rollback FAILED"):
                p.planned_update(self.plan, lambda data: {"outbounds": []})


class DockerOwnershipTests(unittest.TestCase):
    def setUp(self):
        lib._created_containers.clear()
        lib._created_networks.clear()

    def test_cleanup_never_removes_unowned_resources(self):
        with patch.object(lib, "sh") as shell:
            lib.docker_cleanup()
            shell.assert_not_called()

    def test_cleanup_only_removes_owned_resources(self):
        lib._created_containers.add("owned-test")
        lib._created_networks.add("owned-net")
        with patch.object(lib, "sh") as shell:
            lib.docker_cleanup(names=("owned-test", "not-owned"), network="owned-net")
        self.assertEqual(shell.call_count, 2)
        self.assertNotIn("owned-test", lib._created_containers)

    def test_environment_version_overrides_default(self):
        class Cmd:
            stdout = ""
            stderr = ""
            returncode = 0
        seen = []
        def fake_sh(cmd, check_rc=False):
            seen.append(cmd)
            return Cmd()
        with patch.dict("os.environ", {"THREEXUI_VERSION": "latest"}):
            with patch.object(lib, "sh", side_effect=fake_sh), patch.object(lib, "provision_test_login"):
                lib.docker_up()
        self.assertIn(["docker", "pull", "ghcr.io/mhsanaei/3x-ui:latest"], seen)
        publish_args = [part for cmd in seen for part in cmd if part.startswith("127.0.0.1:")]
        self.assertEqual(len(publish_args), 4)

    def test_wrong_foreign_client_uuid_rejected(self):
        class Panel:
            def find_inbound(self, tag):
                return {"port": lib.FOREIGN_INBOUND_PORT, "protocol": "vless", "settings": {
                    "clients": [{"email": lib.SERVICE_CLIENT_EMAIL, "id": "other-uuid"}]}}
        with self.assertRaisesRegex(RuntimeError, "credentials mismatch"):
            lib.ensure_foreign_inbound(Panel(), "expected-uuid")


if __name__ == "__main__":
    unittest.main()
