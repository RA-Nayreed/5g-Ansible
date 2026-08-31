#!/usr/bin/env python3
"""Declarative, non-interactive machine interface for 5g-Ansible."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "fiveg/deployment/v1"
CORES = ("open5gs", "oai", "free5gc")
RANS = ("oai", "srsRAN", "ueransim")
PLATFORMS = ("r2lab", "rfsim")
RUS = {
    "oai": ("benetel1", "benetel2", "jaguar", "panther", "n300", "n320"),
    "srsRAN": ("n300", "n320", "benetel1", "benetel2"),
    "ueransim": ("rfsim",),
}
QHATS = ("qhat01", "qhat02", "qhat03", "qhat10", "qhat11", "qhat20", "qhat21", "qhat22")
QFITS = ("qfit07", "qfit09", "qfit18", "qfit29", "qfit32", "qfit34")
PHONES = ("phone1", "phone2")
SCENARIOS = ("none", "iperf", "interference", "multi-ue-iperf", "ping", "nuttcp")
NODE_FACTS = {
    "sopnode-f1": ("76", "sda1", "ens2f1"),
    "sopnode-f2": ("77", "sda1", "ens2f1"),
    "sopnode-f3": ("95", "sdb2", "ens15f1"),
    "sopnode-w3": ("71", "sda1", "enp59s0f1np1"),
}
PHONE_SERIAL = {"phone1": "MDX0220623006208", "phone2": "34061FDH20068M"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FiveGError(RuntimeError):
    pass


def mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FiveGError(f"{label} must be a mapping")
    return dict(value)


def string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise FiveGError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise FiveGError(f"{label} contains duplicates")
    return list(value)


def bool_value(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise FiveGError(f"{label} must be boolean")
    return value


def node(value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in NODE_FACTS:
        raise FiveGError(f"{label} must be one of: {', '.join(NODE_FACTS)}")
    return value


def load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise FiveGError("YAML specs require PyYAML; JSON specs work without it") from exc
    return yaml.safe_load(text)


def normalize(raw: Any) -> dict[str, Any]:
    spec = mapping(raw, "spec")
    if spec.get("schema") != SCHEMA:
        raise FiveGError(f"schema must be {SCHEMA}")
    deployment_id = spec.get("id")
    if not isinstance(deployment_id, str) or ID_RE.fullmatch(deployment_id) is None:
        raise FiveGError("id must be 1-96 safe characters [A-Za-z0-9._-]")

    core = mapping(spec.get("core"), "core")
    core_type = core.get("type")
    if core_type not in CORES:
        raise FiveGError(f"core.type must be one of: {', '.join(CORES)}")
    core_node = node(core.get("node"), "core.node")

    ran = mapping(spec.get("ran"), "ran")
    ran_type = ran.get("type")
    if ran_type not in RANS:
        raise FiveGError(f"ran.type must be one of: {', '.join(RANS)}")
    ran_node = node(ran.get("node"), "ran.node")
    if core_type == "free5gc" and core_node == ran_node:
        raise FiveGError("free5gc requires different core and RAN nodes")

    platform = mapping(spec.get("platform", {"type": "rfsim"}), "platform")
    platform_type = "rfsim" if ran_type == "ueransim" else platform.get("type", "rfsim")
    if platform_type not in PLATFORMS:
        raise FiveGError(f"platform.type must be one of: {', '.join(PLATFORMS)}")
    ru = "rfsim" if platform_type == "rfsim" else platform.get("ru", "n300")
    if platform_type == "r2lab" and ru not in RUS[ran_type]:
        raise FiveGError(f"platform.ru must be one of: {', '.join(RUS[ran_type])}")
    if ru in ("benetel1", "benetel2") and ran_node != "sopnode-f3":
        raise FiveGError("Benetel currently requires ran.node=sopnode-f3")

    ues = mapping(spec.get("ues", {}), "ues")
    qhats = string_list(ues.get("qhats", []), "ues.qhats")
    qfits = string_list(ues.get("qfits", []), "ues.qfits")
    phones = string_list(ues.get("phones", []), "ues.phones")
    for label, values, allowed in (("ues.qhats", qhats, QHATS), ("ues.qfits", qfits, QFITS), ("ues.phones", phones, PHONES)):
        unknown = sorted(set(values).difference(allowed))
        if unknown:
            raise FiveGError(f"{label} contains unsupported values: {', '.join(unknown)}")
    if platform_type != "r2lab" and (qhats or qfits or phones):
        raise FiveGError("physical UEs require platform.type=r2lab")

    monitoring = mapping(spec.get("monitoring", {}), "monitoring")
    monitoring_enabled = bool_value(monitoring.get("enabled"), "monitoring.enabled", False)
    monitor_node = node(monitoring.get("node", "sopnode-f1"), "monitoring.node") if monitoring_enabled else ""

    profile = spec.get("profile", "default")
    if not isinstance(profile, str) or re.fullmatch(r"[A-Za-z0-9_-]+", profile) is None:
        raise FiveGError("profile has unsafe characters")
    if not (ROOT / "group_vars" / "all" / f"5g_profile_{profile}.yaml").is_file():
        raise FiveGError(f"unknown 5G profile: {profile}")

    reservation = mapping(spec.get("reservation", {}), "reservation")
    reservation_enabled = bool_value(reservation.get("enabled"), "reservation.enabled", True)
    duration = reservation.get("duration_minutes", 120)
    if not isinstance(duration, int) or not 10 <= duration <= 1440:
        raise FiveGError("reservation.duration_minutes must be 10..1440")
    r2lab_mode = reservation.get("r2lab_mode", "require-existing" if platform_type == "r2lab" else "none")
    if r2lab_mode not in ("none", "require-existing", "book"):
        raise FiveGError("reservation.r2lab_mode must be none, require-existing or book")

    deployment = mapping(spec.get("deployment", {}), "deployment")
    extra_vars = mapping(deployment.get("extra_vars", {}), "deployment.extra_vars")
    for key in extra_vars:
        if not isinstance(key, str) or VAR_RE.fullmatch(key) is None:
            raise FiveGError(f"unsafe extra variable name: {key!r}")

    scenario = mapping(spec.get("scenario", {}), "scenario")
    scenario_type = scenario.get("type", "none")
    if scenario_type not in SCENARIOS:
        raise FiveGError(f"scenario.type must be one of: {', '.join(SCENARIOS)}")
    target = node(scenario.get("target_node", core_node), "scenario.target_node") if scenario_type != "none" else ""

    r2lab = mapping(spec.get("r2lab", {}), "r2lab")
    username = r2lab.get("username") or os.environ.get("FIVEG_R2LAB_USERNAME", "")
    if platform_type == "r2lab" and (not isinstance(username, str) or not username):
        raise FiveGError("r2lab.username or FIVEG_R2LAB_USERNAME is required")
    known_hosts = r2lab.get("known_hosts_file", "")
    if not isinstance(known_hosts, str):
        raise FiveGError("r2lab.known_hosts_file must be a string")

    result = {
        "schema": SCHEMA,
        "id": deployment_id,
        "core": {"type": core_type, "node": core_node},
        "ran": {"type": ran_type, "node": ran_node},
        "platform": {"type": platform_type, "ru": ru},
        "ues": {"qhats": qhats, "qfits": qfits, "phones": phones},
        "monitoring": {"enabled": monitoring_enabled, "node": monitor_node},
        "profile": profile,
        "reservation": {"enabled": reservation_enabled, "duration_minutes": duration, "r2lab_mode": r2lab_mode},
        "deployment": {
            "prepare_only": bool_value(deployment.get("prepare_only"), "deployment.prepare_only", False),
            "allow_live_installs": bool_value(deployment.get("allow_live_installs"), "deployment.allow_live_installs", True),
            "manage_os_dependencies": bool_value(deployment.get("manage_os_dependencies"), "deployment.manage_os_dependencies", True),
            "manage_python_dependencies": bool_value(deployment.get("manage_python_dependencies"), "deployment.manage_python_dependencies", True),
            "disruptive_cluster_ops_enabled": bool_value(deployment.get("disruptive_cluster_ops_enabled"), "deployment.disruptive_cluster_ops_enabled", True),
            "k8s_env_enabled": bool_value(deployment.get("k8s_env_enabled"), "deployment.k8s_env_enabled", True),
            "python_interpreter": str(deployment.get("python_interpreter", "")),
            "selected_slices": string_list(deployment.get("selected_slices", []), "deployment.selected_slices"),
            "selected_ues": string_list(deployment.get("selected_ues", []), "deployment.selected_ues"),
            "open5gs_webui_enabled": bool_value(deployment.get("open5gs_webui_enabled"), "deployment.open5gs_webui_enabled", True),
            "open5gs_admin_account_enabled": bool_value(deployment.get("open5gs_admin_account_enabled"), "deployment.open5gs_admin_account_enabled", True),
            "pos_manage_allocation": bool_value(deployment.get("pos_manage_allocation"), "deployment.pos_manage_allocation", True),
            "cleanup_namespaces": string_list(deployment.get("cleanup_namespaces", []), "deployment.cleanup_namespaces"),
            "extra_vars": extra_vars,
        },
        "scenario": {**scenario, "type": scenario_type, "target_node": target},
        "r2lab": {
            "username": username,
            "known_hosts_file": known_hosts,
            "strict_host_key_checking": bool_value(r2lab.get("strict_host_key_checking"), "r2lab.strict_host_key_checking", True),
        },
    }
    if any(v in ("qhat20", "qhat21", "qhat22") for v in qhats):
        result["deployment"]["extra_vars"]["redcap"] = True
    return result


def load_spec(path: Path) -> dict[str, Any]:
    return normalize(load_document(path))


def digest(spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def state_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else (ROOT / ".fiveg").resolve()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def commit() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def node_line(name: str, ru: str, boot_mode: bool = False) -> str:
    suffix, storage, nic = NODE_FACTS[name]
    if name == "sopnode-f3" and ru in ("benetel1", "benetel2"):
        nic = "ens15f1np1"
    line = f"{name} ansible_user=root nic_interface={nic} ip=172.28.2.{suffix} storage={storage}"
    return line + (" boot_mode=live" if boot_mode else "")


def ssh_common(spec: Mapping[str, Any]) -> str:
    r2lab = spec["r2lab"]
    args = [f"-o ProxyJump={r2lab['username']}@faraday.inria.fr"]
    if r2lab["strict_host_key_checking"]:
        args.append("-o StrictHostKeyChecking=yes")
        if r2lab["known_hosts_file"]:
            args.append(f"-o UserKnownHostsFile={r2lab['known_hosts_file']}")
    else:
        args.append("-o StrictHostKeyChecking=no")
    return " ".join(args)


def inventory(spec: Mapping[str, Any]) -> str:
    core_node = spec["core"]["node"]
    ran_node = spec["ran"]["node"]
    core = spec["core"]["type"]
    ran = spec["ran"]["type"]
    ru = spec["platform"]["ru"]
    monitor = spec["monitoring"]
    scenario = spec["scenario"]
    lines = ["[webshell]", "localhost ansible_connection=local", "", "[core_node]", node_line(core_node, ru), "", "[ran_node]", node_line(ran_node, ru, True), "", "[monitor_node]"]
    if monitor["enabled"]:
        lines.append(node_line(monitor["node"], ru))
    target = scenario.get("target_node", "")
    lines += ["", "[iperf_server_node]"]
    if target:
        lines.append(node_line(target, ru))

    if spec["platform"]["type"] == "r2lab":
        user = spec["r2lab"]["username"]
        faraday = f"faraday.inria.fr ansible_user={user}"
        if scenario["type"] == "interference":
            noise = str(scenario.get("noise_usrp", ""))
            faraday += f" interference_usrp={noise if noise in ('n300','n320') else 'fit'} gain={scenario.get('gain',110)} noise_bandwidth={scenario.get('noise_bandwidth','15M')}"
            if str(scenario.get("mode", "TDD")).upper() == "TDD":
                faraday += f" freq={scenario.get('frequency', '3600.00M' if ran == 'srsRAN' else '3411.22M')}"
            else:
                faraday += f" freq_ul={scenario.get('frequency_ul','1747.5M')} freq_dl={scenario.get('frequency_dl','1842.5M')}"
        lines += ["", "[faraday]", faraday]
        common = ssh_common(spec)
        lines += ["", "[qhats]"]
        for ue in spec["ues"]["qhats"]:
            mode = "qmi" if ue in ("qhat20", "qhat21", "qhat22") else "mbim"
            lines.append(f"{ue} ansible_host={ue} ansible_user=root ansible_ssh_common_args='{common}' mode={mode}")
        lines += ["", "[qfits]"]
        for ue in spec["ues"]["qfits"]:
            lines.append(f"{ue} ansible_host={ue} ansible_user=root ansible_ssh_common_args='{common}' mode=mbim")
        lines += ["", "[phones]"]
        for ue in spec["ues"]["phones"]:
            serial = f" serial={PHONE_SERIAL[ue]}" if ue in PHONE_SERIAL else ""
            lines.append(f"{ue} ansible_host=mac{ue} ansible_user=tester ansible_ssh_common_args='{common}' adb_bin=/usr/local/bin/adb{serial}")
        lines += ["", "[phones:vars]", "ansible_connection=local", "ansible_python_interpreter=/usr/bin/python3", "gather_facts=false", "", "[fit_nodes]"]
        if scenario["type"] == "interference":
            fit_map = {"b210": ("fit02", 2, "b210"), "b205mini": ("fit08", 8, "b205")}
            selected: list[str] = []
            for value in (scenario.get("noise_usrp"), scenario.get("viz_usrp")):
                if value in fit_map and value not in selected:
                    selected.append(value)
            for value in selected:
                host, number, kind = fit_map[value]
                lines.append(f"{host} ansible_host={host} ansible_user=root ansible_ssh_common_args='{common}' fit_number={number} fit_usrp={kind}")

    lines += ["", "[sopnodes:children]", "core_node", "ran_node"]
    if monitor["enabled"]:
        lines.append("monitor_node")
    if target and target not in {core_node, ran_node, monitor.get("node", "")}:
        lines.append("iperf_server_node")
    lines += ["", "[k8s_workers:children]", "ran_node"]
    if monitor["enabled"]:
        lines.append("monitor_node")
    lines += ["", "[all:vars]", f'core="{core}"', f'ran="{ran}"', f'core_node_name="{core_node}"', f'ran_node_name="{ran_node}"']
    if monitor["enabled"]:
        lines.append(f'monitor_node_name="{monitor["node"]}"')
    if target:
        lines.append(f'iperf_server_node_name="{target}"')
    lines += [
        'faraday_node_name="faraday.inria.fr"', f'rru="{ru}"',
        f"fhi72={'true' if ru in ('benetel1','benetel2') else 'false'}",
        f"aw2s={'true' if ru in ('jaguar','panther') else 'false'}",
        f"f3_ran={'true' if ran_node == 'sopnode-f3' else 'false'}",
        f"bridge_enabled={'true' if ran_node != core_node else 'false'}",
        f"monitoring_enabled={'true' if monitor['enabled'] else 'false'}",
    ]
    return "\n".join(lines) + "\n"


def extra_vars(spec: Mapping[str, Any]) -> dict[str, Any]:
    dep = spec["deployment"]
    result = {
        "fiveg_profile": spec["profile"], "fiveg_deployment_id": spec["id"],
        "fiveg_prepare_only": dep["prepare_only"], "fiveg_allow_live_installs": dep["allow_live_installs"],
        "fiveg_manage_os_dependencies": dep["manage_os_dependencies"], "fiveg_manage_python_dependencies": dep["manage_python_dependencies"],
        "fiveg_disruptive_cluster_ops_enabled": dep["disruptive_cluster_ops_enabled"], "fiveg_k8s_env_enabled": dep["k8s_env_enabled"],
        "fiveg_python_interpreter": dep["python_interpreter"], "fiveg_selected_slices": dep["selected_slices"], "fiveg_selected_ues": dep["selected_ues"],
        "open5gs_webui_enabled": dep["open5gs_webui_enabled"], "open5gs_admin_account_enabled": dep["open5gs_admin_account_enabled"],
        "pos_manage_allocation": dep["pos_manage_allocation"], "fiveg_cleanup_namespaces": dep["cleanup_namespaces"],
        "r2lab_strict_host_key_checking": spec["r2lab"]["strict_host_key_checking"], "r2lab_known_hosts_file": spec["r2lab"]["known_hosts_file"],
    }
    if spec["scenario"]["type"] == "interference":
        result.update({"run_interference_test": True, "noise_usrp": spec["scenario"].get("noise_usrp", ""), "viz_usrp": spec["scenario"].get("viz_usrp", "")})
    result.update(dep["extra_vars"])
    return result


def run(command: list[str], log: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout, encoding="utf-8")
    if check and result.returncode:
        raise FiveGError(f"command failed ({result.returncode}): {shlex.join(command)}" + (f"; see {log}" if log else ""))
    return result


def playbook(inv: Path, vars_path: Path, name: str) -> list[str]:
    return ["ansible-playbook", "-i", str(inv), "-e", f"@{vars_path}", str(ROOT / name)]


def reserve(spec: Mapping[str, Any], state: dict[str, Any]) -> None:
    if not spec["reservation"]["enabled"]:
        return
    nodes = {spec["core"]["node"], spec["ran"]["node"]}
    if spec["monitoring"]["enabled"]:
        nodes.add(spec["monitoring"]["node"])
    if spec["scenario"].get("target_node"):
        nodes.add(spec["scenario"]["target_node"])
    ordered = sorted(nodes)
    result = run(["pos", "calendar", "create", "-d", str(spec["reservation"]["duration_minutes"]), "-s", "now", *ordered], check=False)
    rid = result.stdout.strip()
    if result.returncode or not rid or rid == "-1":
        raise FiveGError(f"SLICES reservation failed: {result.stdout.strip()}")
    state["slices_reservation"] = {"id": rid, "nodes": ordered}


def r2lab_authority(spec: Mapping[str, Any], state: dict[str, Any]) -> None:
    if spec["platform"]["type"] != "r2lab" or spec["reservation"]["r2lab_mode"] == "none":
        return
    user = spec["r2lab"]["username"]
    if spec["reservation"]["r2lab_mode"] == "require-existing":
        result = run(["ssh", f"{user}@faraday.inria.fr", "rhubarbe", "leases", "--check"], check=False)
        if result.returncode:
            raise FiveGError("R2Lab lease check failed")
        state["r2lab_reservation"] = {"mode": "existing"}
        return
    email, password = os.environ.get("FIVEG_R2LAB_EMAIL", ""), os.environ.get("FIVEG_R2LAB_PASSWORD", "")
    if not email or not password:
        raise FiveGError("r2lab_mode=book requires FIVEG_R2LAB_EMAIL and FIVEG_R2LAB_PASSWORD")
    now = datetime.now(timezone.utc).astimezone()
    start = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    end = start + timedelta(minutes=spec["reservation"]["duration_minutes"])
    remote = ["rhubarbe", "book", start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"), "-e", email, "-p", password, "-s", user, "-v"]
    result = subprocess.run(["ssh", f"{user}@faraday.inria.fr", *remote], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise FiveGError("R2Lab booking failed")
    state["r2lab_reservation"] = {"mode": "booked", "start": start.isoformat(), "end": end.isoformat()}


def release(state: Mapping[str, Any]) -> None:
    item = state.get("slices_reservation")
    if isinstance(item, Mapping) and item.get("id") and isinstance(item.get("nodes"), list):
        run(["pos", "calendar", "delete", "--id", str(item["id"]), *[str(v) for v in item["nodes"]]], check=False)


def runtime_files(spec: Mapping[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    inv, var = directory / "hosts.ini", directory / "vars.json"
    inv.write_text(inventory(spec), encoding="utf-8")
    write_json(var, extra_vars(spec)); write_json(directory / "spec.json", spec)
    return inv, var


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True)); return
    for key, item in value.items():
        print(f"{key}: {json.dumps(item, sort_keys=True) if isinstance(item, (dict,list)) else item}")


def capabilities(args: argparse.Namespace) -> int:
    emit({"schema": "fiveg/capabilities/v1", "cores": list(CORES), "rans": list(RANS), "platforms": list(PLATFORMS), "rus_by_ran": {k:list(v) for k,v in RUS.items()}, "ues": {"qhats":list(QHATS),"qfits":list(QFITS),"phones":list(PHONES)}, "monitoring":["5G-Monarch"], "scenarios":list(SCENARIOS), "profiles": sorted(p.stem.removeprefix("5g_profile_") for p in (ROOT/"group_vars"/"all").glob("5g_profile_*.yaml")), "advanced": "deployment.extra_vars passes any existing Ansible feature without a second support matrix"}, args.json)
    return 0


def plan(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec)); directory = state_root(args.state_root) / spec["id"]
    commands = [["ansible-galaxy","install","-r","collections/requirements.yml"]]
    if spec["platform"]["type"] == "r2lab": commands.append(["ansible-playbook","-i",str(directory/"hosts.ini"),"-e",f"@{directory/'vars.json'}","playbooks/deploy_r2lab.yml"])
    commands.append(["ansible-playbook","-i",str(directory/"hosts.ini"),"-e",f"@{directory/'vars.json'}","playbooks/deploy.yml"])
    emit({"schema":"fiveg/deployment-plan/v1","id":spec["id"],"spec_sha256":digest(spec),"state_directory":str(directory),"commands":commands,"spec":spec}, args.json); return 0


def up(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec)); directory = state_root(args.state_root) / spec["id"]
    if directory.exists() and not args.resume: raise FiveGError(f"state exists: {directory}; use --resume")
    inv, var = runtime_files(spec, directory)
    state = {"schema":"fiveg/deployment-state/v1","id":spec["id"],"spec_sha256":digest(spec),"fiveg_ansible_commit":commit(),"state":"preparing"}
    if (directory/"state.json").is_file() and args.resume: state = json.loads((directory/"state.json").read_text())
    write_json(directory/"state.json", state)
    try:
        if not args.resume: reserve(spec, state); r2lab_authority(spec, state); write_json(directory/"state.json", state)
        run(["ansible-galaxy","install","-r",str(ROOT/"collections/requirements.yml")], directory/"collections.log")
        if spec["platform"]["type"] == "r2lab": state["state"]="r2lab-preparing"; write_json(directory/"state.json",state); run(playbook(inv,var,"playbooks/deploy_r2lab.yml"),directory/"r2lab.log")
        state["state"]="deploying"; write_json(directory/"state.json",state); run(playbook(inv,var,"playbooks/deploy.yml"),directory/"deploy.log")
        state["state"]="ready"; write_json(directory/"state.json",state)
        manifest={"schema":"fiveg/deployment-manifest/v1","id":spec["id"],"state":"ready","spec_sha256":state["spec_sha256"],"fiveg_ansible_commit":state["fiveg_ansible_commit"],"core":spec["core"],"ran":spec["ran"],"platform":spec["platform"],"ues":spec["ues"],"monitoring":spec["monitoring"],"profile":spec["profile"],"state_directory":str(directory)}
        write_json(directory/"manifest.json",manifest); emit(manifest,args.json); return 0
    except Exception:
        state["state"]="failed"; write_json(directory/"state.json",state); raise


def load_state(args: argparse.Namespace) -> tuple[dict[str,Any],dict[str,Any],Path]:
    directory=state_root(args.state_root)/args.deployment
    try: return json.loads((directory/"state.json").read_text()), json.loads((directory/"spec.json").read_text()), directory
    except FileNotFoundError as exc: raise FiveGError(f"unknown deployment: {args.deployment}") from exc


def status(args: argparse.Namespace) -> int:
    state,spec,directory=load_state(args)
    probe=run(["ansible","-i",str(directory/"hosts.ini"),"core_node","-b","-m","shell","-a","kubectl get nodes -o name 2>/dev/null; kubectl get pods -A --no-headers 2>/dev/null || true"],check=False)
    emit({"schema":"fiveg/deployment-status/v1","id":args.deployment,"state":state.get("state","unknown"),"observation_returncode":probe.returncode,"observation":probe.stdout,"spec":spec},args.json); return 0 if probe.returncode==0 else 2


def down(args: argparse.Namespace) -> int:
    state,spec,directory=load_state(args); state["state"]="stopping"; write_json(directory/"state.json",state)
    result=run(playbook(directory/"hosts.ini",directory/"vars.json","playbooks/down.yml"),directory/"down.log",check=False); release(state)
    state["state"]="stopped" if result.returncode==0 else "cleanup-failed"; write_json(directory/"state.json",state)
    emit({"schema":"fiveg/deployment-down/v1","id":args.deployment,"state":state["state"],"returncode":result.returncode},args.json); return 0 if result.returncode==0 else 2


SCENARIO_PLAYBOOKS={"iperf":("playbooks/setup_iperf.yml","playbooks/run_scenario_iperf.yml"),"interference":("playbooks/setup_interference.yml","playbooks/run_scenario_interference.yml"),"multi-ue-iperf":("playbooks/setup_iperf.yml","playbooks/run_scenario_iperf_multi.yml"),"ping":("playbooks/setup_iperf.yml","playbooks/run_scenario_ping.yml"),"nuttcp":("playbooks/setup_iperf.yml","playbooks/run_scenario_nuttcp.yml")}
def scenario(args: argparse.Namespace) -> int:
    _,spec,directory=load_state(args); kind=args.type or spec["scenario"]["type"]
    if kind not in SCENARIO_PLAYBOOKS: raise FiveGError("a concrete scenario type is required")
    setup,target=SCENARIO_PLAYBOOKS[kind]
    if not args.no_setup: run(playbook(directory/"hosts.ini",directory/"vars.json",setup),directory/f"scenario-{kind}-setup.log")
    run(playbook(directory/"hosts.ini",directory/"vars.json",target),directory/f"scenario-{kind}.log")
    emit({"schema":"fiveg/scenario-result/v1","deployment_id":args.deployment,"type":kind,"state":"completed"},args.json); return 0


def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(prog="fiveg",description=__doc__); sub=parser.add_subparsers(dest="command",required=True)
    cap=sub.add_parser("capabilities"); cap.add_argument("--json",action="store_true"); cap.set_defaults(func=capabilities)
    p=sub.add_parser("plan"); p.add_argument("--spec",required=True); p.add_argument("--state-root"); p.add_argument("--json",action="store_true"); p.set_defaults(func=plan)
    u=sub.add_parser("up"); u.add_argument("--spec",required=True); u.add_argument("--state-root"); u.add_argument("--resume",action="store_true"); u.add_argument("--json",action="store_true"); u.set_defaults(func=up)
    s=sub.add_parser("status"); s.add_argument("--deployment",required=True); s.add_argument("--state-root"); s.add_argument("--json",action="store_true"); s.set_defaults(func=status)
    d=sub.add_parser("down"); d.add_argument("--deployment",required=True); d.add_argument("--state-root"); d.add_argument("--json",action="store_true"); d.set_defaults(func=down)
    r=sub.add_parser("scenario"); r.add_argument("--deployment",required=True); r.add_argument("--state-root"); r.add_argument("--type",choices=list(SCENARIO_PLAYBOOKS)); r.add_argument("--no-setup",action="store_true"); r.add_argument("--json",action="store_true"); r.set_defaults(func=scenario)
    return parser


def main(argv: list[str] | None=None) -> int:
    args=build_parser().parse_args(argv)
    try: return int(args.func(args))
    except (FiveGError,OSError,json.JSONDecodeError) as exc: print(f"fiveg: {exc}",file=sys.stderr); return 2

if __name__=="__main__": raise SystemExit(main())
