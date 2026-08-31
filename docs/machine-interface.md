# Declarative machine interface

`bin/fiveg` is the non-interactive interface for controllers and automation. It uses the same Ansible roles and playbooks as the interactive `deploy.sh`; it does not maintain a second 5G implementation.

## Commands

```bash
bin/fiveg capabilities --json
bin/fiveg plan --spec examples/deployment-machine.yaml --json
bin/fiveg up --spec examples/deployment-machine.yaml --json
bin/fiveg status --deployment example-open5gs-srsran-n300 --json
bin/fiveg scenario --deployment example-open5gs-srsran-n300 --type ping --json
bin/fiveg down --deployment example-open5gs-srsran-n300 --json
```

The default state root is `.fiveg/<deployment-id>/`. Each deployment stores the normalized spec, generated inventory, Ansible variables, command logs, state and the final manifest.

## Capability ownership

The machine interface exposes the deployment choices already owned by this repository: Open5GS/OAI/Free5GC cores; OAI/srsRAN/UERANSIM RANs; RFSIM and R2Lab; the existing R2Lab RU and UE families; 5G-Monarch; profiles; and the existing iperf, interference, multi-UE iperf, ping and nuttcp scenarios. `deployment.extra_vars` is intentionally passed through so existing advanced Ansible features do not need to be mirrored in a controller-specific support matrix.

## Deployment policy

Machine callers can express policy without rewriting the checked-out source:

- `deployment.prepare_only`: prepare infrastructure but do not deploy core/RAN workloads.
- `deployment.allow_live_installs`: permit roles to install missing tool dependencies such as Helm/yq.
- `deployment.manage_os_dependencies`: let roles install OS packages they own.
- `deployment.manage_python_dependencies`: let roles create/install Python runtimes they own.
- `deployment.disruptive_cluster_ops_enabled`: permit kubelet/CoreDNS restart operations in Open5GS deployment.
- `deployment.k8s_env_enabled`: enable the `setup/k8s/k8s_env` role.
- `deployment.python_interpreter`: use a caller-prepared Python runtime.
- `deployment.selected_slices`: empty means the whole profile; otherwise materialize only the named slices where the role supports selection.
- `deployment.selected_ues`: empty means the whole profile; otherwise materialize only the named Open5GS subscribers.
- `deployment.open5gs_webui_enabled` / `open5gs_admin_account_enabled`: optional Open5GS UI/admin operations.
- `deployment.pos_manage_allocation`: whether the POS role owns allocation free/allocate operations.
- `deployment.cleanup_namespaces`: namespaces that `down` is explicitly authorized to delete. An empty list deletes no Kubernetes namespace.

Defaults preserve the historic interactive behavior.

## R2Lab authority and cleanup

`reservation.r2lab_mode` is one of:

- `require-existing`: check `rhubarbe leases --check`; this is the default for R2Lab machine deployments.
- `book`: create a booking using `FIVEG_R2LAB_EMAIL` and `FIVEG_R2LAB_PASSWORD`; secrets are never persisted in deployment state.
- `none`: skip R2Lab lease handling when authority is owned elsewhere.

R2Lab cleanup is selected-resource only. The cleanup role stops only UEs present in the current inventory and powers off only the selected `rru`. The previous global `all-off` operation is intentionally removed.

## SSH policy

The interactive path remains backwards-compatible. The machine interface defaults `r2lab.strict_host_key_checking` to `true` and generates strict ProxyJump inventory arguments. Direct UE-stop SSH commands use the same explicit strict/non-strict policy variable.

## Advanced features

Use `deployment.extra_vars` for variables already supported by the Ansible roles, for example `redcap`, `csi_logger_enabled`, or `oai_gnb_mode`. This keeps 5g-Ansible as the capability authority instead of creating a second hard-coded feature matrix in a caller.
