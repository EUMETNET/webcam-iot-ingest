"""Low-cardinality metrics and health endpoints for ingestion workers."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class WorkerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.epochs = Counter(
            "webcam_ingestion_epoch_total", "Ingestion epochs", ["source_network", "result"], registry=self.registry
        )
        self.epoch_duration = Histogram(
            "webcam_ingestion_epoch_duration_seconds", "Epoch duration", ["source_network"], registry=self.registry
        )
        self.jobs = Counter(
            "webcam_ingestion_job_total", "Ingestion job outcomes", ["source_network", "result"], registry=self.registry
        )
        self.selected = Gauge(
            "webcam_source_stream_due_count", "Jobs selected in the current epoch", ["source_network"], registry=self.registry
        )
        self.active = Gauge(
            "webcam_ingestion_active_jobs", "Currently active jobs", ["source_network"], registry=self.registry
        )
        self.outbox = Gauge(
            "webcam_publication_outbox_pending_count", "Pending durable publications", ["source_network"], registry=self.registry
        )
        self.retries = Counter(
            "webcam_image_retry_total", "Controlled retry/future replay events", ["source_network", "operation", "reason"], registry=self.registry
        )
        self.stage_duration = Histogram(
            "webcam_ingestion_stage_duration_seconds",
            "Wall-clock duration of ingestion stages",
            ["source_network", "stage", "outcome"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.job_duration = Histogram(
            "webcam_ingestion_source_job_duration_seconds",
            "End-to-end source job duration",
            ["source_network", "outcome"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
            registry=self.registry,
        )
        self.image_latency = Histogram(
            "webcam_ingestion_image_latency_seconds",
            "Provider, download, and completed-publication latency",
            ["source_network", "measure"],
            buckets=(1, 5, 10, 30, 60, 120, 180, 300, 365, 600, 900, 1800, 3600, 21600, 86400),
            registry=self.registry,
        )
        self.transformations = Counter(
            "webcam_ingestion_transformation_total",
            "Transformation outcomes",
            ["source_network", "transformation_version", "outcome"],
            registry=self.registry,
        )
        self.derived_images = Counter(
            "webcam_ingestion_derived_image_total",
            "Successfully produced derived images",
            ["source_network", "transformation_version"],
            registry=self.registry,
        )
        self.derived_size = Histogram(
            "webcam_ingestion_derived_image_size_bytes",
            "Produced derived image size",
            ["source_network", "transformation_version"],
            buckets=(5_000, 10_000, 25_000, 50_000, 100_000, 200_000, 500_000),
            registry=self.registry,
        )
        self.derived_width = Histogram(
            "webcam_ingestion_derived_image_width_pixels",
            "Produced derived image width",
            ["source_network", "transformation_version"],
            buckets=(160, 320, 640, 1024, 1920, 3840),
            registry=self.registry,
        )
        self.derived_height = Histogram(
            "webcam_ingestion_derived_image_height_pixels",
            "Produced derived image height",
            ["source_network", "transformation_version"],
            buckets=(120, 240, 288, 480, 720, 1080, 2160),
            registry=self.registry,
        )
        self.mqtt_payload_size = Histogram(
            "webcam_ingestion_mqtt_payload_size_bytes",
            "Built MQTT payload size",
            ["source_network", "transformation_version"],
            buckets=(256, 512, 1024, 2048, 4096, 8192, 16384),
            registry=self.registry,
        )
        self.failures = Counter(
            "webcam_ingestion_stage_failure_total",
            "Controlled ingestion failure reasons",
            ["source_network", "stage", "reason"],
            registry=self.registry,
        )

    def observe_stage(self, stage: str, outcome: str, duration_s: float) -> None:
        self.stage_duration.labels("win", stage, outcome).observe(duration_s)

    def observe_event(self, event: str, values: dict[str, object]) -> None:
        if event == "job_completed":
            self.job_duration.labels("win", str(values["outcome"])).observe(
                float(values["duration_s"])
            )
        elif event == "image_latency":
            self.image_latency.labels("win", str(values["measure"])).observe(
                float(values["duration_s"])
            )
        elif event == "transformation":
            self.transformations.labels(
                "win", str(values["version"]), str(values["outcome"])
            ).inc()
        elif event == "derived_image":
            labels = ("win", str(values["version"]))
            self.derived_images.labels(*labels).inc()
            self.derived_size.labels(*labels).observe(float(values["size_bytes"]))
            self.derived_width.labels(*labels).observe(float(values["width"]))
            self.derived_height.labels(*labels).observe(float(values["height"]))
        elif event == "mqtt_payload":
            self.mqtt_payload_size.labels(
                "win", str(values["version"])
            ).observe(float(values["size_bytes"]))
        elif event == "failure":
            self.failures.labels(
                "win", str(values["stage"]), str(values["reason"])
            ).inc()


@dataclass
class WorkerHealth:
    intake_enabled: bool = True
    last_epoch_success_monotonic: float | None = None
    readiness_window_s: float = 60.0

    def ready(self) -> bool:
        if not self.intake_enabled or self.last_epoch_success_monotonic is None:
            return False
        return time.monotonic() - self.last_epoch_success_monotonic <= self.readiness_window_s


class HealthServer:
    def __init__(self, host: str, port: int, health: WorkerHealth, metrics: WorkerMetrics) -> None:
        self._health = health
        self._metrics = metrics
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/livez":
                    self._send(200, b"live\n", "text/plain")
                elif self.path == "/readyz":
                    self._send(200 if outer._health.ready() else 503, b"ready\n" if outer._health.ready() else b"not ready\n", "text/plain")
                elif self.path == "/metrics":
                    self._send(200, generate_latest(outer._metrics.registry), "text/plain; version=0.0.4")
                else:
                    self._send(404, b"not found\n", "text/plain")

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_port

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
