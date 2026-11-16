#!/usr/bin/env python3
"""Generates the "SPARK + JODA" Grafana dashboard -> grafana/dashboards/hosts.json.

Layout follows TMobileSandbox 70_monitoring/make_dashboard.py (L40S). Every node
series carries a `host` label (SPARK | JODA) set in prometheus.yml.tmpl.
GPU panels use nvidia_gpu_exporter (nvidia-smi); GB10 has unified memory, so
there is no VRAM panel: GPU allocations show up in SPARK RAM.
"""
import json
import pathlib

DS = {"type": "prometheus", "uid": "prom"}
panels, y = [], 0
TRAFFIC = [(None, "green"), (70, "yellow"), (90, "red")]
HOST_COLORS = {"SPARK": "purple", "JODA": "orange"}


def p(title, exprs, unit, w=8, h=7, kind="timeseries", mx=None, thresholds=None, desc=None):
    global y
    x = sum(q["gridPos"]["w"] for q in panels if q["gridPos"]["y"] == y)
    if x + w > 24:
        y += h
        x = 0
    fc = {"unit": unit, "min": 0, **({"max": mx} if mx is not None else {})}
    if kind == "timeseries":
        fc["custom"] = {"fillOpacity": 12, "lineWidth": 2, "showPoints": "never"}
    if thresholds:
        fc["thresholds"] = {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in thresholds]}
        if kind == "stat":
            fc["color"] = {"mode": "thresholds"}
    overrides = [{"matcher": {"id": "byName", "options": h},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                 for h, c in HOST_COLORS.items()] if kind == "timeseries" else []
    panel = {"type": kind, "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
             "fieldConfig": {"defaults": fc, "overrides": overrides},
             "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                         "reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value_and_name"},
             "targets": [{"expr": e, "legendFormat": l, "refId": chr(65 + i), "datasource": DS}
                         for i, (e, l) in enumerate(exprs)]}
    if desc:
        panel["description"] = desc
    panels.append(panel)


def row(title):
    global y
    y += max((q["gridPos"]["h"] for q in panels if q["gridPos"]["y"] == y), default=0)
    panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}})
    y += 1


RAM = "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"
DISKS = '{fstype=~"ext4|xfs|btrfs", mountpoint!~"/boot.*"}'
DISK_USED = f"(1 - node_filesystem_avail_bytes{DISKS} / node_filesystem_size_bytes{DISKS}) * 100"
CPU = '100 * (1 - avg by (host) (rate(node_cpu_seconds_total{mode="idle"}[1m])))'
GPU = 'nvidia_smi_utilization_gpu_ratio * 100'

row("Stav teď")
p("RAM", [(RAM, "{{host}}")], "percent", 4, 5, "stat", 100, TRAFFIC)
p("Disky zaplnění", [(DISK_USED, "{{host}} {{mountpoint}}")], "percent", 4, 5, "stat", 100, TRAFFIC)
p("Swap použitý", [("node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes", "{{host}}")], "bytes", 4, 5, "stat")
p("CPU", [(CPU, "{{host}}")], "percent", 4, 5, "stat", 100, TRAFFIC)
p("GPU využití", [(GPU, "")], "percent", 2, 5, "gauge", 100, TRAFFIC)
p("GPU teplota", [("nvidia_smi_temperature_gpu", "")], "celsius", 2, 5, "stat",
  thresholds=[(None, "green"), (80, "yellow"), (90, "red")])
p("GPU příkon", [("nvidia_smi_power_draw_watts", "")], "watt", 2, 5, "stat")
p("Uptime", [("time() - node_boot_time_seconds", "{{host}}")], "s", 2, 5, "stat")

row("SPARK GPU")
p("GPU využití", [(GPU, "compute"), ("nvidia_smi_utilization_memory_ratio * 100", "paměťová sběrnice")], "percent", 8, 8, mx=100)
p("GPU teplota a rezerva", [("nvidia_smi_temperature_gpu", "teplota"),
                            ("nvidia_smi_temperature_gpu_tlimit", "rezerva do throttlingu")], "celsius", 8, 8,
  desc="tlimit = kolik °C zbývá do teplotního omezení.")
p("GPU příkon a takt", [("nvidia_smi_power_draw_watts", "příkon W"),
                        ("nvidia_smi_clocks_current_sm_clock_hz / 1e6", "SM MHz")], "short", 8, 8,
  desc="Takt je zamčený přes -lgc 2100 (spark-hard-poweroff); pokles = throttling.")

row("Paměť a CPU")
p("RAM použito", [("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "{{host}}")], "bytes", 8, 8,
  desc="Na SPARKu sem patří i GPU alokace (unified memory).")
p("Swap", [("node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes", "{{host}}")], "bytes", 8, 8)
p("Tlak na paměť (PSI full)", [("rate(node_pressure_memory_stalled_seconds_total[1m]) * 100", "{{host}}")], "percent", 8, 8,
  desc="% času, kdy všechny procesy čekají na paměť. spark-oom-guard zasahuje od 15 % po 10 s.")
p("CPU", [(CPU, "{{host}}")], "percent", 8, 8, mx=100)
p("Load / jádra", [('node_load1 / on (host) count by (host) (node_cpu_seconds_total{mode="idle"})', "{{host}}")], "percentunit", 8, 8)
p("Teplota CPU (max čidlo)", [("max by (host) (node_hwmon_temp_celsius)", "{{host}}")], "celsius", 8, 8)

row("Disk a síť")
p("Disk / volno", [('node_filesystem_avail_bytes{mountpoint="/"}', "{{host}}")], "bytes", 8, 8)
p("Další disky volno", [('node_filesystem_avail_bytes{fstype=~"ext4|xfs|btrfs", mountpoint!="/", mountpoint!~"/boot.*"}', "{{host}} {{mountpoint}}"),
                        ('max by (host) (node_filesystem_avail_bytes{fstype="zfs"})', "{{host}} zfs pool")], "bytes", 8, 8)
p("Síť (kabel)", [('rate(node_network_receive_bytes_total{device=~"en.*|eth.*"}[1m]) and on (host, device) node_network_up == 1', "{{host}} {{device}} příjem"),
                  ('rate(node_network_transmit_bytes_total{device=~"en.*|eth.*"}[1m]) and on (host, device) node_network_up == 1', "{{host}} {{device}} odesílání")], "Bps", 8, 8,
  desc="Gigabit = ~118 MB/s strop.")

dash = {"uid": "hosts", "title": "SPARK + JODA", "tags": ["infra"], "timezone": "browser", "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"}, "schemaVersion": 39, "panels": panels}
out = pathlib.Path(__file__).parent / "grafana/dashboards/hosts.json"
out.write_text(json.dumps(dash, ensure_ascii=False, indent=1) + "\n")
print(out, len(panels), "panels")
