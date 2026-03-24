"""Prometheus metrics exporter plugin for casectl.

Exposes system metrics in Prometheus text exposition format at GET /metrics
for scraping by Prometheus, Grafana Agent, or compatible collectors.
"""

from casectl.plugins.prometheus.plugin import PrometheusPlugin

__all__ = ["PrometheusPlugin"]
