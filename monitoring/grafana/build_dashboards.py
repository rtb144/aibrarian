# ruff: noqa: E501, E741
"""Generate the TV-playlist dashboards (aibrarian-{pipeline,library,gpu,host}.json).

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


def pipeline():
    return dash("aibrarian-pipeline", "aibrarian · Pipeline", [
        panel("stat", "Items not yet cataloged", "Media items still in the pipeline (excl. failed/skipped/needs_review). Postgres via API.",
              0, 0, 6, 7, [(f"max(aib_backlog_items{{{API}}})", "not cataloged")]),
        panel("stat", "Failed items", "Media items failed after max attempts. Postgres via API.",
              6, 0, 6, 7, [(f'max(aib_media_items{{{API},stage="failed"}})', "failed")]),
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
              8, 0, 8, 9, [('sum(collections_vector_total{job="qdrant"})', "vectors")]),
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


if __name__ == "__main__":
    here = Path(__file__).parent
    for name, fn in [("pipeline", pipeline), ("library", library), ("gpu", gpu), ("host", host)]:
        _id = 0
        (here / f"aibrarian-{name}.json").write_text(json.dumps(fn(), indent=2) + "\n")
