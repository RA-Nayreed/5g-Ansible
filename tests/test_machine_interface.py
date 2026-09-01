from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
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

    def rfsim_spec(self):
        raw = self.base_spec()
        raw["platform"] = {"type": "rfsim", "ru": "rfsim"}
        raw["ues"] = {"qhats": [], "qfits": [], "phones": []}
        raw["reservation"] = {"enabled": True, "duration_minutes": 120, "r2lab_mode": "none"}
        raw["provider"] = {
            "manage": True,
            "project": "post5g-beta",
            "experiment": "test-deployment",
            "experiment_duration": "4h",
        }
        return raw

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

    def test_provider_context_retries_prefix_after_creation(self):
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
        sleeps = []
        prefix_attempt = 0
        original_run = fiveg.run
        original_sleep = fiveg.time.sleep

        def fake_run(command, log=None, check=True):
            nonlocal prefix_attempt
            calls.append(tuple(command))
            if command[:3] == ["slices", "experiment", "show"]:
                return subprocess.CompletedProcess(command, 1, "")
            if command[:3] == ["post5g", "experiment", "prefix"]:
                prefix_attempt += 1
                if prefix_attempt < 3:
                    return subprocess.CompletedProcess(command, 1, "")
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
        fiveg.time.sleep = sleeps.append
        try:
            state = {}
            fiveg.provider_context(spec, state, create_missing=True)
        finally:
            fiveg.run = original_run
            fiveg.time.sleep = original_sleep

        self.assertIn(("slices", "project", "use", "post5g-beta"), calls)
        self.assertIn(("slices", "experiment", "create", "sran", "--duration", "4h"), calls)
        self.assertEqual(
            3,
            calls.count(("post5g", "experiment", "prefix", "sran")),
        )
        self.assertEqual([5.0, 5.0], sleeps)
        self.assertTrue(state["provider"]["experiment_created"])
        self.assertEqual(state["provider"]["network"]["lb"], "198.51.100.10")

    def test_provider_failure_retains_partial_provider_identity(self):
        raw = self.base_spec()
        raw["provider"] = {
            "manage": True,
            "project": "post5g-beta",
            "experiment": "sran",
            "experiment_duration": "4h",
        }
        spec = fiveg.normalize(raw)
        original_run = fiveg.run
        original_sleep = fiveg.time.sleep

        def fake_run(command, log=None, check=True):
            if command[:3] == ["slices", "experiment", "show"]:
                return subprocess.CompletedProcess(command, 1, "")
            if command[:3] == ["post5g", "experiment", "prefix"]:
                return subprocess.CompletedProcess(command, 1, "")
            return subprocess.CompletedProcess(command, 0, "ok")

        fiveg.run = fake_run
        fiveg.time.sleep = lambda _seconds: None
        try:
            state = {}
            with self.assertRaisesRegex(fiveg.FiveGError, "after 12 attempt"):
                fiveg.provider_context(spec, state, create_missing=True)
        finally:
            fiveg.run = original_run
            fiveg.time.sleep = original_sleep

        self.assertEqual("post5g-beta", state["provider"]["project"])
        self.assertEqual("sran", state["provider"]["experiment"])
        self.assertTrue(state["provider"]["experiment_created"])
        self.assertNotIn("network", state["provider"])

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

    def test_resume_reestablishes_missing_reservation(self):
        raw = self.rfsim_spec()
        spec = fiveg.normalize(raw)
        calls = []
        original_provider = fiveg.provider_context
        original_reserve = fiveg.reserve
        original_run = fiveg.run

        def fake_provider(_spec, state, *, create_missing):
            self.assertFalse(create_missing)
            state["provider"] = {
                "type": "slices",
                "project": "post5g-beta",
                "experiment": "test-deployment",
                "experiment_created": True,
                "network": {
                    "subnet": "198.51.100.0/24",
                    "lb": "198.51.100.10",
                    "expiration_time": "2030-01-01T00:00:00Z",
                },
            }

        def fake_reserve(_spec, state):
            calls.append("reserve")
            state["slices_reservation"] = {"id": "reservation-1", "nodes": ["sopnode-f2", "sopnode-f3"]}

        def fake_run(command, log=None, check=True):
            return subprocess.CompletedProcess(command, 0, "ok")

        fiveg.provider_context = fake_provider
        fiveg.reserve = fake_reserve
        fiveg.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                spec_path = root / "spec.json"
                spec_path.write_text(json.dumps(spec), encoding="utf-8")
                state_root = root / "state"
                directory = state_root / spec["id"]
                directory.mkdir(parents=True)
                fiveg.write_json(
                    directory / "state.json",
                    {
                        "schema": "fiveg/deployment-state/v1",
                        "id": spec["id"],
                        "spec_sha256": fiveg.digest(spec),
                        "fiveg_ansible_commit": "fixture",
                        "state": "failed",
                        "failure": {"phase": "provider", "message": "fixture"},
                    },
                )
                args = SimpleNamespace(
                    spec=str(spec_path),
                    state_root=str(state_root),
                    resume=True,
                    json=True,
                )
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(0, fiveg.up(args))
                resumed = json.loads((directory / "state.json").read_text())
        finally:
            fiveg.provider_context = original_provider
            fiveg.reserve = original_reserve
            fiveg.run = original_run

        self.assertEqual(["reserve"], calls)
        self.assertEqual("ready", resumed["state"])
        self.assertNotIn("failure", resumed)
        self.assertEqual("reservation-1", resumed["slices_reservation"]["id"])

    def test_up_persists_failure_phase(self):
        raw = self.rfsim_spec()
        spec = fiveg.normalize(raw)
        original_provider = fiveg.provider_context

        def fail_provider(_spec, _state, *, create_missing):
            self.assertTrue(create_missing)
            raise fiveg.FiveGError("provider fixture failed")

        fiveg.provider_context = fail_provider
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                spec_path = root / "spec.json"
                spec_path.write_text(json.dumps(spec), encoding="utf-8")
                state_root = root / "state"
                args = SimpleNamespace(
                    spec=str(spec_path),
                    state_root=str(state_root),
                    resume=False,
                    json=True,
                )
                with self.assertRaisesRegex(fiveg.FiveGError, "provider fixture failed"):
                    fiveg.up(args)
                failed = json.loads(
                    (state_root / spec["id"] / "state.json").read_text()
                )
        finally:
            fiveg.provider_context = original_provider

        self.assertEqual("failed", failed["state"])
        self.assertEqual("provider", failed["failure"]["phase"])
        self.assertEqual("provider fixture failed", failed["failure"]["message"])

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
