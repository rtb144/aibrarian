# ruff: noqa: E501, E741
"""Generate the TV-playlist dashboards (aibrarian-{pipeline,library,gpu,host,containers,profiling}.json).

Run: python monitoring/grafana/build_dashboards.py  (pure stdlib, writes next to this file).
Each dashboard fits one 1080p screen (<= 28 grid rows), refresh 30s, last 6h,
tag 'aibrarian' so a Grafana playlist can add them by tag.
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
API = 'job="aibrarian-api"'
GPU0 = "GPU0 RTX 3060 (whisper)"
GPU1 = "GPU1 RTX 5060 Ti (Ollama)"
_id = 0


def panel(kind, title, desc, x, y, w, h, targets, unit=None, **extra):
    global _id
    _id += 1
    p = {"id": _id, "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "fieldConfig": {"defaults": {}, "overrides": []},
         "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": l, "datasource": DS}
                     for i, (e, l) in enumerate(targets)]}
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                        "textMode": "value_and_name", "colorMode": "background",
                        "graphMode": "area", "justifyMode": "center"}
    elif kind == "timeseries":
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}}
        p["fieldConfig"]["defaults"]["custom"] = {"lineWidth": 2, "fillOpacity": 10}
    p.update(extra)
    return p


def gpu_overrides():
    return {"overrides": [
        {"matcher": {"id": "byName", "options": GPU0}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]},
        {"matcher": {"id": "byName", "options": GPU1}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}]}


def gpu_targets(metric, extra=""):
    return [(f'max(DCGM_FI_DEV_{metric}{{gpu="0"{extra}}})', GPU0),
            (f'max(DCGM_FI_DEV_{metric}{{gpu="1"{extra}}})', GPU1)]


def dash(uid, title, panels):
    return {"__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource",
                          "pluginId": "prometheus", "pluginName": "Prometheus"}],
            "__requires": [{"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"}],
            "uid": uid, "title": title, "tags": ["aibrarian"], "schemaVersion": 39, "version": 1,
            "time": {"from": "now-6h", "to": "now"}, "refresh": "30s", "timezone": "browser",
            "editable": True, "graphTooltip": 1, "panels": panels}



def fixed_color(color):
    """fieldConfig defaults for a fixed-color stat (thresholds base matches)."""
    return {
        "defaults": {
            "color": {"mode": "fixed", "fixedColor": color},
            "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]},
        },
        "overrides": [],
    }


def pipeline():
    return dash("aibrarian-pipeline", "aibrarian · Pipeline", [
        panel("stat", "Items not yet cataloged", "Media items still in the pipeline (excl. failed/skipped/needs_review). Postgres via API.",
              0, 0, 6, 7, [(f"max(aib_backlog_items{{{API}}})", "not cataloged")],
              fieldConfig=fixed_color("yellow")),
        panel("stat", "Failed items", "Media items failed after max attempts. Postgres via API.",
              6, 0, 6, 7, [(f'max(aib_media_items{{{API},stage="failed"}})', "failed")],
              fieldConfig=fixed_color("red")),
        panel("stat", "Needs review", "Items waiting for manual review. Postgres via API.",
              12, 0, 6, 7, [(f'max(aib_media_items{{{API},stage="needs_review"}})', "needs review")]),
        panel("stat", "Actions / hour", "Pipeline actions completed in the last hour, all workers.",
              18, 0, 6, 7, [("sum(increase(aib_action_total[1h]))", "actions/h")]),
        panel("bargauge", "Items per stage", "Current count of media items in each pipeline stage. Postgres via API.",
              0, 7, 8, 21, [(f"max by (stage) (aib_media_items{{{API}}})", "{{stage}}")],
              options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                       "reduceOptions": {"calcs": ["lastNotNull"], "values": False}}),
        panel("timeseries", "Action rate (per min) by action", "Pipeline actions completed per minute, summed over all workers.",
              8, 7, 8, 11, [("sum by (action) (rate(aib_action_total[5m])) * 60", "{{action}}")]),
        panel("timeseries", "Action failures / hour", "Failed action executions per hour, summed over all workers.",
              16, 7, 8, 11, [('sum by (action) (increase(aib_action_total{outcome="failed"}[1h]))', "{{action}}")]),
        panel("timeseries", "p95 action duration", "95th percentile duration per action over 30m, all workers.",
              8, 18, 8, 10, [("histogram_quantile(0.95, sum by (le, action) (rate(aib_action_duration_seconds_bucket[30m])))", "{{action}}")], unit="s"),
        panel("stat", "Scrape targets up", "1 = Prometheus can scrape the target, 0 = down.",
              16, 18, 8, 10, [('max by (job, instance) (up{job=~"aibrarian.*|qdrant"})', "{{job}} {{instance}}")],
              ),
    ])


def library():
    return dash("aibrarian-library", "aibrarian · Library", [
        panel("stat", "Open suggestions (total)", "Open librarian suggestions across all domains. Postgres via API.",
              0, 0, 8, 9, [(f"sum(max by (domain) (aib_suggestions_open{{{API}}}))", "open")]),
        panel("stat", "Qdrant vectors", "Total vectors stored across all Qdrant collections.",
              8, 0, 8, 9, [('sum(collections_vector_total{job="qdrant"})', "vectors")],
              fieldConfig=fixed_color("blue")),
        panel("stat", "Qdrant collections", "Number of Qdrant collections.",
              16, 0, 8, 9, [('max(collections_total{job="qdrant"})', "collections")]),
        panel("bargauge", "Open suggestions by domain", "Open librarian suggestions per domain. Postgres via API.",
              0, 9, 8, 19, [(f"max by (domain) (aib_suggestions_open{{{API}}})", "{{domain}}")],
              options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                       "reduceOptions": {"calcs": ["lastNotNull"], "values": False}}),
        panel("timeseries", "Open suggestions over time", "Open suggestions per domain over time.",
              8, 9, 16, 9, [(f"max by (domain) (aib_suggestions_open{{{API}}})", "{{domain}}")]),
        panel("timeseries", "Qdrant vectors over time", "Total vectors in Qdrant (growth = embedding progress).",
              8, 18, 16, 10, [('sum(collections_vector_total{job="qdrant"})', "vectors")]),
    ])


def gpu():
    g = []
    for i, (m, title, unit, desc) in enumerate([
            ("GPU_UTIL", "Utilization", "percent", "GPU core utilization (DCGM)."),
            ("FB_USED", "Memory used", "decmbytes", "Framebuffer memory used. GPU1 ~0 while cataloging = Ollama fell back to CPU."),
            ("GPU_TEMP", "Temperature", "celsius", "GPU temperature."),
            ("POWER_USAGE", "Power", "watt", "GPU power draw.")]):
        g.append(panel("stat", f"{title} now", desc, i * 6, 0, 6, 8, gpu_targets(m), unit=unit))
        g.append(panel("timeseries", title, desc, (i % 2) * 12, 8 + (i // 2) * 10, 12, 10,
                       gpu_targets(m), unit=unit))
    for p in g:
        p["fieldConfig"].update(gpu_overrides())
    return dash("aibrarian-gpu", "aibrarian · GPUs", g)


def host():
    fs = 'fstype!~"tmpfs|overlay|squashfs",mountpoint=~"/|/opt.*"'
    return dash("aibrarian-host", "aibrarian · Host (z8g4)", [
        panel("stat", "CPU busy", "Host CPU busy %, all cores.", 0, 0, 6, 7,
              [('100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))', "cpu")], unit="percent"),
        panel("stat", "Memory used", "Host memory used % (excl. cache/buffers).", 6, 0, 6, 7,
              [("100 * (1 - max(node_memory_MemAvailable_bytes) / max(node_memory_MemTotal_bytes))", "mem")], unit="percent"),
        panel("stat", "Load (1m)", "1-minute load average.", 12, 0, 6, 7, [("max(node_load1)", "load1")]),
        panel("stat", "Root disk used", "Usage % of /.", 18, 0, 6, 7,
              [('100 * (1 - max(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}) / max(node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}))', "/")], unit="percent"),
        panel("timeseries", "CPU & memory", "CPU busy % and memory used %.", 0, 7, 12, 10,
              [('100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))', "cpu %"),
               ("100 * (1 - max(node_memory_MemAvailable_bytes) / max(node_memory_MemTotal_bytes))", "mem %")], unit="percent"),
        panel("timeseries", "Load average", "Load average 1/5/15m.", 12, 7, 12, 10,
              [("max(node_load1)", "1m"), ("max(node_load5)", "5m"), ("max(node_load15)", "15m")]),
        panel("bargauge", "Disk usage", "Filesystem usage % per mountpoint.", 0, 17, 12, 11,
              [(f"100 * (1 - max by (mountpoint) (node_filesystem_avail_bytes{{{fs}}}) / max by (mountpoint) (node_filesystem_size_bytes{{{fs}}}))", "{{mountpoint}}")],
              unit="percent", options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                                       "reduceOptions": {"calcs": ["lastNotNull"], "values": False}}),
        panel("timeseries", "Network throughput", "Receive (+) / transmit (-) bytes/s, physical interfaces.", 12, 17, 12, 11,
              [('sum(rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[5m]))', "rx"),
               ('-sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[5m]))', "tx")], unit="Bps"),
    ])


RUNNING = "(max by (name) (docker_container_running) == 1)"


def _map(v, text, color):
    return {"type": "value", "options": {str(v): {"text": text, "color": color}}}


def containers():
    """Docker container health (textfile metrics from monitoring/container-health) + cAdvisor,
    with cAdvisor panels joined on docker_container_running == 1 so stopped containers vanish."""
    def stat(title, desc, x, expr, steps):
        p = panel("stat", title, desc, x, 0, 4, 5, [(expr, title)])
        p["options"]["textMode"] = "value"
        p["options"]["graphMode"] = "none"
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
            {"color": c, "value": v} for v, c in steps]}
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
        return p
    base = [None, "green"]
    stats = [
        stat("Running", "Containers in running state.", 0, "sum(docker_container_running) or vector(0)", [base]),
        stat("Unhealthy", "Containers whose Docker healthcheck reports unhealthy.", 4,
             'sum(docker_container_health_status{status="unhealthy"}) or vector(0)', [base, (1, "red")]),
        stat("Starting", "Containers whose healthcheck is still in 'starting'.", 8,
             'sum(docker_container_health_status{status="starting"}) or vector(0)', [base, (1, "yellow")]),
        stat("Stopped", "Containers that exist but are not running (exited/created).", 12,
             "count(docker_container_running == 0) or vector(0)", [[None, "text"], (1, "#808080")]),
        stat("Restarts (1h)", "Docker restart-policy restarts over the last hour (all containers).", 16,
             "sum(clamp_min(delta(docker_container_restart_count[1h]), 0)) or vector(0)", [base, (1, "yellow"), (5, "red")]),
        stat("Exporter age", "Seconds since docker-health-textfile last ran (timer runs every 30s).", 20,
             "time() - max(docker_health_textfile_last_run_timestamp)", [base, (90, "yellow"), (300, "red")]),
    ]
    stats[-1]["fieldConfig"]["defaults"]["unit"] = "s"

    table = panel("table", "Containers", "All containers: health (Docker healthcheck), running state, restarts, uptime.",
                  0, 5, 12, 23, [
                      ("max by (name, compose_project) (docker_container_healthy)", ""),
                      ("max by (name) (docker_container_running)", ""),
                      ("max by (name) (docker_container_restart_count)", ""),
                      (f"time() - max by (name) (docker_container_started_seconds) and on (name) {RUNNING}", "")])
    for t in table["targets"]:
        t.update({"instant": True, "format": "table"})
    table["transformations"] = [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True},
            "indexByName": {"name": 0, "compose_project": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4, "Value #D": 5},
            "renameByName": {"name": "Container", "compose_project": "Project", "Value #A": "Health",
                             "Value #B": "State", "Value #C": "Restarts", "Value #D": "Uptime"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Health"}]}}]
    table["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    cell = {"type": "color-background", "mode": "basic"}
    table["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": "Health"}, "properties": [
            {"id": "mappings", "value": [_map(1, "healthy", "green"), _map(0, "unhealthy", "red"),
                                         _map(0.5, "starting", "yellow"), _map(-1, "none", "#808080")]},
            {"id": "custom.cellOptions", "value": cell}, {"id": "custom.width", "value": 110}]},
        {"matcher": {"id": "byName", "options": "State"}, "properties": [
            {"id": "mappings", "value": [_map(1, "running", "green"), _map(0, "stopped", "#808080")]},
            {"id": "custom.cellOptions", "value": cell}, {"id": "custom.width", "value": 100}]},
        {"matcher": {"id": "byName", "options": "Restarts"}, "properties": [
            {"id": "custom.width", "value": 90}]},
        {"matcher": {"id": "byName", "options": "Uptime"}, "properties": [
            {"id": "unit", "value": "s"}, {"id": "decimals", "value": 0}]}]

    cpu = panel("timeseries", "CPU per running container (cores)",
                "cAdvisor CPU usage, only containers currently running (joined with docker_container_running == 1).",
                12, 5, 12, 11,
                [(f'sum by (name) (rate(container_cpu_usage_seconds_total{{name!=""}}[2m])) and on (name) {RUNNING}', "{{name}}")])
    mem = panel("timeseries", "Memory per running container",
                "cAdvisor working-set memory, only containers currently running.", 12, 16, 12, 12,
                [(f'max by (name) (container_memory_working_set_bytes{{name!=""}}) and on (name) {RUNNING}', "{{name}}")],
                unit="bytes")
    return dash("aibrarian-containers", "aibrarian · Containers (z8g4)", stats + [table, cpu, mem])


PYRO = {"type": "grafana-pyroscope-datasource", "uid": "${ds_pyroscope}"}
CPU_PROFILE = "process_cpu:cpu:nanoseconds:cpu:nanoseconds"
# pyroscope-io sends application_name ("aibrarian" by default) as Pyroscope's service_name;
# our own tags (service, stage, host, prefect_run, media_id) arrive as plain labels.
SEL = '{service_name="${app}", service=~"${service:regex}", stage=~"${stage:regex}"}'


def _pyro_var(name, label):
    q = {"type": "labelValue", "profileTypeId": CPU_PROFILE, "labelName": name, "refId": name}
    return {"name": name, "label": label, "type": "query", "datasource": PYRO, "query": q,
            "definition": f"labelValue {name}", "refresh": 2, "includeAll": True, "multi": True,
            "allValue": ".*", "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "sort": 1}


def profiling():
    def t(qtype, **kw):
        return {"refId": "A", "datasource": PYRO, "queryType": qtype, "profileTypeId": CPU_PROFILE,
                "labelSelector": SEL, "groupBy": [], **kw}
    base = {"datasource": PYRO, "fieldConfig": {"defaults": {}, "overrides": []}}
    panels = [
        {**base, "id": 1, "type": "timeseries", "title": "CPU by stage",
         "description": "CPU time per second by stage tag (Pyroscope), filtered by service/stage.",
         "gridPos": {"x": 0, "y": 20, "w": 24, "h": 8},
         "targets": [t("metrics", groupBy=["stage"])],
         "fieldConfig": {"defaults": {"unit": "ns", "custom": {"lineWidth": 2, "fillOpacity": 10, "stacking": {"mode": "normal"}}}, "overrides": []},
         "options": {"legend": {"displayMode": "list", "placement": "right"}}},
        {**base, "id": 2, "type": "flamegraph", "title": "CPU flame graph",
         "description": "Pyroscope CPU flame graph for the selected service(s) and stage(s).",
         "gridPos": {"x": 0, "y": 0, "w": 24, "h": 20},
         "targets": [t("profile")]},
    ]
    templ = [
        {"name": "ds_pyroscope", "label": "Pyroscope", "type": "datasource",
         "query": "grafana-pyroscope-datasource", "refresh": 1, "hide": 0},
        {"name": "app", "label": "service_name", "type": "custom", "query": "aibrarian",
         "current": {"selected": True, "text": "aibrarian", "value": "aibrarian"},
         "options": [{"selected": True, "text": "aibrarian", "value": "aibrarian"}]},
        _pyro_var("service", "service"),
        _pyro_var("stage", "stage"),
    ]
    return {"uid": "aibrarian-profiling", "title": "aibrarian · Profiling", "tags": ["aibrarian"],
            "schemaVersion": 39, "version": 1, "time": {"from": "now-1h", "to": "now"},
            "refresh": "1m", "timezone": "browser", "editable": True, "graphTooltip": 1,
            "templating": {"list": templ}, "panels": panels}


if __name__ == "__main__":
    here = Path(__file__).parent
    for name, fn in [("pipeline", pipeline), ("library", library), ("gpu", gpu), ("host", host), ("containers", containers), ("profiling", profiling)]:
        _id = 0
        (here / f"aibrarian-{name}.json").write_text(json.dumps(fn(), indent=2) + "\n")
