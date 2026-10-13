#!/usr/bin/env bash
# docker-health-textfile.sh - export Docker container state/health as node_exporter textfile metrics.
# Host-level tooling for z8g4 (lives in the aibrarian repo for convenience).
#
# Env:
#   OUTPUT        target .prom file (default /opt/stacks/monitoring/node_textfile/docker_health.prom)
#                 use OUTPUT=- to print to stdout (dry run)
#   DOCKER        docker binary (default: docker)
#   MOCK_INSPECT  path to a file with pre-rendered inspect lines (tests / no docker)
# Needs: bash, docker CLI, awk, GNU date. No jq.
set -euo pipefail
export LC_ALL=C

OUTPUT="${OUTPUT:-/opt/stacks/monitoring/node_textfile/docker_health.prom}"
DOCKER="${DOCKER:-docker}"

# One line per container, tab separated:
# name project service image running health restarts startedAt oomKilled exitCode
FMT='{{.Name}}	{{index .Config.Labels "com.docker.compose.project"}}	{{index .Config.Labels "com.docker.compose.service"}}	{{.Config.Image}}	{{.State.Running}}	{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}	{{.RestartCount}}	{{.State.StartedAt}}	{{.State.OOMKilled}}	{{.State.ExitCode}}'

collect() {
  if [[ -n "${MOCK_INSPECT:-}" ]]; then
    cat -- "$MOCK_INSPECT"; return
  fi
  local ids
  ids=$(timeout 20 "$DOCKER" ps -aq --no-trunc) || return 1
  [[ -z "$ids" ]] && return 0
  # shellcheck disable=SC2086  # word splitting of ids is intended
  timeout 20 "$DOCKER" inspect --format "$FMT" $ids 2>/dev/null || true  # container may vanish between ps and inspect
}

raw=$(collect) || { echo "docker-health-textfile: docker query failed" >&2; exit 1; }

# Convert StartedAt (RFC3339) to epoch in one date call (GNU date -f).
started=""
if [[ -n "$raw" ]]; then
  started=$(printf '%s\n' "$raw" | awk -F'\t' '{ s=$8; if (s ~ /^0001-/ || s=="") s="1970-01-01T00:00:00Z"; print s }' \
            | date -f - +%s 2>/dev/null) || started=""
fi

now=$(date +%s)
render() {
  paste <(printf '%s\n' "$raw") <(printf '%s\n' "$started") | awk -F'\t' -v now="$now" '
  function esc(s) { gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); gsub(/\n/, "\\n", s); return s }
  BEGIN {
    print "# HELP docker_container_running 1 if the container is running."
    print "# TYPE docker_container_running gauge"
  }
  NF >= 10 {
    n++
    name=$1; sub(/^\//, "", name)
    L = sprintf("name=\"%s\",compose_project=\"%s\",compose_service=\"%s\",image=\"%s\"", esc(name), esc($2), esc($3), esc($4))
    run[n]   = sprintf("docker_container_running{%s} %d", L, ($5=="true"))
    h = $6; if (h !~ /^(healthy|unhealthy|starting|none)$/) h = "none"
    hv = (h=="healthy") ? 1 : (h=="unhealthy") ? 0 : (h=="starting") ? 0.5 : -1
    hs[n] = ""
    split("healthy unhealthy starting none", st, " ")
    for (i = 1; i <= 4; i++) hs[n] = hs[n] sprintf("docker_container_health_status{%s,status=\"%s\"} %d\n", L, st[i], (st[i]==h))
    hg[n]  = sprintf("docker_container_healthy{%s} %s", L, hv)
    rc[n]  = sprintf("docker_container_restart_count{%s} %d", L, $7+0)
    ss = ($11 ~ /^[0-9]+$/) ? $11 : 0
    sa[n]  = sprintf("docker_container_started_seconds{%s} %d", L, ss)
    oom[n] = sprintf("docker_container_oom_killed{%s} %d", L, ($9=="true"))
    ec[n]  = sprintf("docker_container_exit_code{%s} %d", L, $10+0)
  }
  END {
    for (i = 1; i <= n; i++) print run[i]
    print "# HELP docker_container_health_status Docker healthcheck status (one-hot; status=none means no healthcheck)."
    print "# TYPE docker_container_health_status gauge"
    for (i = 1; i <= n; i++) printf "%s", hs[i]
    print "# HELP docker_container_healthy 1 healthy, 0 unhealthy, 0.5 starting, -1 no healthcheck."
    print "# TYPE docker_container_healthy gauge"
    for (i = 1; i <= n; i++) print hg[i]
    print "# HELP docker_container_restart_count Docker RestartCount for the container."
    print "# TYPE docker_container_restart_count gauge"
    for (i = 1; i <= n; i++) print rc[i]
    print "# HELP docker_container_started_seconds Unix time the container was last started (0 = never)."
    print "# TYPE docker_container_started_seconds gauge"
    for (i = 1; i <= n; i++) print sa[i]
    print "# HELP docker_container_oom_killed 1 if the last exit was an OOM kill."
    print "# TYPE docker_container_oom_killed gauge"
    for (i = 1; i <= n; i++) print oom[i]
    print "# HELP docker_container_exit_code Last exit code of the container."
    print "# TYPE docker_container_exit_code gauge"
    for (i = 1; i <= n; i++) print ec[i]
    print "# HELP docker_health_textfile_containers Number of containers exported."
    print "# TYPE docker_health_textfile_containers gauge"
    printf "docker_health_textfile_containers %d\n", n
    print "# HELP docker_health_textfile_last_run_timestamp Unix time of the last successful run."
    print "# TYPE docker_health_textfile_last_run_timestamp gauge"
    printf "docker_health_textfile_last_run_timestamp %d\n", now
  }'
}

if [[ "$OUTPUT" == "-" ]]; then
  render
  exit 0
fi

dir=$(dirname -- "$OUTPUT")
tmp=$(mktemp "$dir/.docker_health.prom.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
render > "$tmp"
chmod 0644 "$tmp"
mv -f -- "$tmp" "$OUTPUT"
trap - EXIT
