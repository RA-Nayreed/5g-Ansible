from __future__ import annotations

import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
EVENTS_PATH = ROOT / "tools" / "fiveg_events.py"


def load_events():
    spec = importlib.util.spec_from_file_location("fiveg_events_test", EVENTS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MachineEventTests(unittest.TestCase):
    def test_event_channel_is_versioned_jsonl_with_upstream_message(self):
        events = load_events()
        stream = StringIO()
        emitter = events.EventEmitter(enabled=True, stream=stream)
        emitter.emit(
            "deployment-001",
            "provider",
            "completed",
            component="slices",
            detail={"project": "project-a", "experiment": "experiment-a"},
        )
        payload = json.loads(stream.getvalue())
        self.assertEqual("fiveg/event/v1", payload["schema"])
        self.assertEqual("deployment-001", payload["deployment_id"])
        self.assertEqual("provider", payload["phase"])
        self.assertEqual("completed", payload["event"])
        self.assertEqual("SLICES provider", payload["message"])
        self.assertEqual("slices", payload["component"])
        self.assertEqual("project-a", payload["detail"]["project"])

    def test_deployment_events_do_not_parse_or_replay_ansible_text(self):
        events = load_events()
        stream = StringIO()

        def fake_run(command, log=None, check=True):
            del check
            return subprocess.CompletedProcess(
                command,
                0,
                "PLAY [deployment]\nTASK [install everything]\nchanged: [node]\n",
            )

        events.core.run = fake_run
        events.install_event_wrappers(events.EventEmitter(enabled=True, stream=stream))
        log = Path("/tmp/deployment-001/deploy.log")
        result = events.core.run(["ansible-playbook", "deploy.yml"], log=log)
        self.assertEqual(0, result.returncode)
        payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(["started", "completed"], [item["event"] for item in payloads])
        self.assertTrue(all(item["phase"] == "deployment" for item in payloads))
        self.assertTrue(all(item["message"] == "5G deployment" for item in payloads))
        self.assertTrue(all(item["component"] == "5g-stack" for item in payloads))
        rendered = stream.getvalue()
        self.assertNotIn("PLAY", rendered)
        self.assertNotIn("TASK", rendered)
        self.assertNotIn("changed:", rendered)

    def test_provider_event_reports_experiment_but_not_provider_network(self):
        events = load_events()
        stream = StringIO()

        def fake_provider(spec, state, *, create_missing):
            del create_missing
            state["provider"] = {
                "type": "slices",
                "project": spec["provider"]["project"],
                "experiment": spec["provider"]["experiment"],
                "experiment_created": False,
                "network": {
                    "subnet": "198.51.100.0/24",
                    "lb": "198.51.100.10",
                    "expiration_time": "2030-01-01T00:00:00Z",
                },
            }

        events.core.provider_context = fake_provider
        events.install_event_wrappers(events.EventEmitter(enabled=True, stream=stream))
        spec = {
            "id": "deployment-001",
            "provider": {"project": "project-a", "experiment": "experiment-a"},
        }
        state = {}
        events.core.provider_context(spec, state, create_missing=True)
        payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(["started", "completed"], [item["event"] for item in payloads])
        self.assertEqual("SLICES provider experiment experiment-a", payloads[-1]["message"])
        self.assertEqual("project-a", payloads[-1]["detail"]["project"])
        self.assertEqual("experiment-a", payloads[-1]["detail"]["experiment"])
        self.assertFalse(payloads[-1]["detail"]["experiment_created"])
        # Provider-assigned network data belongs in state/manifest, not progress chatter.
        self.assertNotIn("subnet", stream.getvalue())
        self.assertNotIn("198.51.100.10", stream.getvalue())

    def test_events_flag_is_facade_only_and_not_forwarded_to_core_parser(self):
        events = load_events()
        seen = []
        events.install_event_wrappers = lambda _emitter: seen.append("installed")
        events.core.main = lambda argv: seen.append(tuple(argv)) or 0
        self.assertEqual(0, events.main(["up", "--spec", "x.json", "--events", "--json"]))
        self.assertEqual("installed", seen[0])
        self.assertEqual(("up", "--spec", "x.json", "--json"), seen[1])


if __name__ == "__main__":
    unittest.main()
