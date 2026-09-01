# Machine progress events

`bin/fiveg` keeps stdout reserved for the final machine result. Callers that want live semantic progress may add `--events`; the command then emits newline-delimited JSON records on stderr using schema `fiveg/event/v1`.

Example:

```text
bin/fiveg up --spec request.json --state-root .fiveg --json --events
```

The event channel describes only operations owned by 5g-Ansible, for example provider context, SLICES reservation, Ansible collection preparation, physical-resource preparation, 5G deployment, scenario execution, and cleanup.

A representative record is:

```json
{"schema":"fiveg/event/v1","deployment_id":"run-001","phase":"deployment","event":"started","component":"5g-stack"}
```

The event channel is intentionally semantic. It does not parse or replay Ansible `PLAY`, `TASK`, handler, host-change, or module-result text. Detailed Ansible output remains in the run-owned evidence logs such as `collections.log`, `r2lab.log`, `deploy.log`, `down.log`, and scenario logs.

Provider network assignments such as subnet, load-balancer address, and expiration remain in provider state and the final deployment manifest rather than progress messages.

Consumers must treat stdout as the authoritative final machine result and `fiveg/event/v1` records as progress only. Unknown event phases or fields should be ignored rather than interpreted as deployment truth.
