from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fiveg_machine", ROOT / "tools" / "fiveg_machine.py")
assert SPEC and SPEC.loader
fiveg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fiveg)


class MachineInterfaceTests(unittest.TestCase):
    def base_spec(self):
        return {
            "schema": "fiveg/deployment/v1",
            "id": "test-deployment",
            "core": {"type": "open5gs", "node": "sopnode-f2"},
            "ran": {"type": "srsRAN", "node": "sopnode-f3"},
            "platform": {"type": "r2lab", "ru": "n300"},
            "ues": {"qhats": [], "qfits": ["qfit07"], "phones": []},
            "monitoring": {"enabled": False},
            "profile": "default",
            "reservation": {"enabled": False, "r2lab_mode": "none"},
            "deployment": {
                "allow_live_installs": False,
                "manage_os_dependencies": False,
                "manage_python_dependencies": False,
                "disruptive_cluster_ops_enabled": False,
                "python_interpreter": "/opt/controller-venv/bin/python",
                "selected_slices": ["slice1"],
                "selected_ues": ["uesim01"],
                "open5gs_webui_enabled": False,
                "open5gs_admin_account_enabled": False,
                "pos_manage_allocation": False,
            },
            "scenario": {"type": "none"},
            "r2lab": {"username": "testslice", "strict_host_key_checking": True},
        }

    def test_normalize_and_inventory_preserve_full_selection(self):
        spec = fiveg.normalize(self.base_spec())
        self.assertEqual(spec["core"]["type"], "open5gs")
        self.assertEqual(spec["ran"]["type"], "srsRAN")
        self.assertEqual(spec["deployment"]["selected_slices"], ["slice1"])
        self.assertFalse(spec["provider"]["manage"])
        inventory = fiveg.inventory(spec)
        self.assertIn('rru="n300"', inventory)
        self.assertIn("qfit07", inventory)
        self.assertIn("StrictHostKeyChecking=yes", inventory)

    def test_extra_vars_replace_source_rewriting_controls(self):
        spec = fiveg.normalize(self.base_spec())
        values = fiveg.extra_vars(spec)
        self.assertFalse(values["fiveg_allow_live_installs"])
        self.assertFalse(values["fiveg_disruptive_cluster_ops_enabled"])
        self.assertFalse(values["open5gs_webui_enabled"])
        self.assertFalse(values["pos_manage_allocation"])
        self.assertEqual(values["fiveg_selected_slices"], ["slice1"])
        self.assertEqual(values["fiveg_selected_ues"], ["uesim01"])

    def test_provider_context_is_normalized_and_owned_by_machine_up(self):
        raw = self.base_spec()
        raw["provider"] = {
            "manage": True,
            "project": "post5g-beta",
            "experiment": "sran",
            "experiment_duration": "4h",
        }
        spec = fiveg.normalize(raw)
        self.assertEqual(spec["provider"]["project"], "post5g-beta")
        self.assertEqual(spec["provider"]["experiment"], "sran")

        calls = []
        original = fiveg.run

        def fake_run(command, log=None, check=True):
            calls.append(tuple(command))
            if command[:3] == ["slices", "experiment", "show"]:
                return subprocess.CompletedProcess(command, 1, "")
            if command[:3] == ["post5g", "experiment", "prefix"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({
                        "subnet": "198.51.100.0/24",
                        "lb": "198.51.100.10",
                        "expiration_time": "2030-01-01T00:00:00Z",
                    }),
                )
            return subprocess.CompletedProcess(command, 0, "ok")

        fiveg.run = fake_run
        try:
            state = {}
            fiveg.provider_context(spec, state, create_missing=True)
        finally:
            fiveg.run = original

        self.assertIn(("slices", "project", "use", "post5g-beta"), calls)
        self.assertIn(("slices", "experiment", "create", "sran", "--duration", "4h"), calls)
        self.assertIn(("post5g", "experiment", "prefix", "sran"), calls)
        self.assertTrue(state["provider"]["experiment_created"])
        self.assertEqual(state["provider"]["network"]["lb"], "198.51.100.10")

    def test_provider_resume_fails_if_experiment_disappeared(self):
        raw = self.base_spec()
        raw["provider"] = {"manage": True, "project": "post5g-beta", "experiment": "sran"}
        spec = fiveg.normalize(raw)
        original = fiveg.run

        def fake_run(command, log=None, check=True):
            if command[:3] == ["slices", "experiment", "show"]:
                return subprocess.CompletedProcess(command, 1, "")
            return subprocess.CompletedProcess(command, 0, "ok")

        fiveg.run = fake_run
        try:
            with self.assertRaises(fiveg.FiveGError):
                fiveg.provider_context(spec, {}, create_missing=False)
        finally:
            fiveg.run = original

    def test_free5gc_rejects_colocated_ran(self):
        raw = self.base_spec()
        raw["core"] = {"type": "free5gc", "node": "sopnode-f3"}
        with self.assertRaises(fiveg.FiveGError):
            fiveg.normalize(raw)

    def test_global_r2lab_cleanup_is_gone(self):
        cleanup = (ROOT / "roles" / "r2lab" / "cleanup" / "tasks" / "main.yml").read_text()
        self.assertNotIn("all-off", cleanup)
        self.assertIn("rhubarbe pdu off", cleanup)
        self.assertIn("r2lab_selected_ues", cleanup)

    def test_machine_interface_has_no_controller_specific_names(self):
        text = (ROOT / "tools" / "fiveg_machine.py").read_text()
        self.assertNotIn("synthran", text.lower())


if __name__ == "__main__":
    unittest.main()
