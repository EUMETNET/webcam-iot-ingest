import urllib.error
import urllib.request
import pytest
from prometheus_client import generate_latest

from ingestion.worker_metrics import HealthServer, WorkerHealth, WorkerMetrics


def test_structured_job_observability_is_low_cardinality() -> None:
    metrics = WorkerMetrics()
    metrics.observe_event(
        "job_completed", {"outcome": "published", "duration_s": 1.5}
    )
    for measure, duration in (
        ("provider_to_download", 120),
        ("download_to_job_end", 2),
        ("provider_to_job_end", 122),
    ):
        metrics.observe_event(
            "image_latency", {"measure": measure, "duration_s": duration}
        )
    metrics.observe_event(
        "transformation", {"version": "T0V0", "outcome": "success"}
    )
    metrics.observe_event(
        "source_download_bytes", {"size_bytes": 60000}
    )
    metrics.observe_event(
        "s3_upload_bytes", {"size_bytes": 12000}
    )
    metrics.observe_event("s3_operation", {"result": "success"})
    metrics.observe_event(
        "mqtt_operation",
        {"version": "T0V0", "result": "success"},
    )
    metrics.observe_event(
        "retry",
        {"operation": "mqtt_publish", "reason": "request_failure"},
    )
    metrics.observe_event("marker_unchanged_skip", {})
    metrics.observe_event(
        "source_image",
        {
            "size_bytes": 54321,
            "width": 1280,
            "height": 720,
            "format": "JPEG",
            "color_mode": "RGB",
            "color_depth_bits": 24,
        },
    )
    metrics.observe_event(
        "derived_image",
        {
            "version": "T0V0",
            "size_bytes": 12345,
            "width": 400,
            "height": 224,
            "format": "JPEG",
            "color_mode": "RGB",
            "color_depth_bits": 24,
        },
    )
    metrics.observe_event(
        "mqtt_payload", {"version": "T0V0", "size_bytes": 1024}
    )
    metrics.observe_event(
        "failure", {"stage": "s3_upload", "reason": "s3_upload"}
    )
    metrics.observe_event(
        "freshness_batch",
        {
            "requested_streams": 51,
            "returned_streams": 50,
            "missing_streams": 1,
            "successful_requests": 1,
            "failed_requests": 1,
            "throttled_requests": 1,
            "batch_size": 50,
        },
    )

    body = generate_latest(metrics.registry)
    for metric_name in (
        b"webcam_ingestion_source_job_duration_seconds",
        b"webcam_ingestion_image_latency_seconds",
        b"webcam_ingestion_transformation_total",
        b"webcam_ingestion_source_image_total",
        b"webcam_ingestion_source_download_bytes_total",
        b"webcam_ingestion_s3_upload_bytes_total",
        b"webcam_s3_upload_total",
        b"webcam_mqtt_publication_total",
        b"webcam_image_retry_total",
        b"webcam_ingestion_marker_unchanged_skip_total",
        b"webcam_ingestion_source_image_size_bytes",
        b"webcam_ingestion_source_image_width_pixels",
        b"webcam_ingestion_source_image_height_pixels",
        b"webcam_ingestion_source_image_color_depth_bits",
        b"webcam_ingestion_derived_image_total",
        b"webcam_ingestion_derived_image_size_bytes",
        b"webcam_ingestion_derived_image_width_pixels",
        b"webcam_ingestion_derived_image_height_pixels",
        b"webcam_ingestion_derived_image_color_depth_bits",
        b"webcam_ingestion_derived_image_metadata_total",
        b"webcam_ingestion_mqtt_payload_size_bytes",
        b"webcam_ingestion_stage_failure_total",
        b"webcam_ingestion_freshness_batch_total",
        b"webcam_ingestion_freshness_batch_stream_total",
        b"webcam_ingestion_freshness_batch_size",
    ):
        assert metric_name in body
    assert b"source_stream_id" not in body


def test_health_and_metrics_endpoints() -> None:
    health = WorkerHealth()
    metrics = WorkerMetrics()
    try:
        server = HealthServer("127.0.0.1", 0, health, metrics)
    except PermissionError:
        pytest.skip("sandbox forbids loopback socket binding")
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        assert urllib.request.urlopen(base + "/livez").status == 200
        try:
            urllib.request.urlopen(base + "/readyz")
            raise AssertionError("unready endpoint unexpectedly succeeded")
        except urllib.error.HTTPError as error:
            assert error.code == 503
        health.last_epoch_success_monotonic = __import__("time").monotonic()
        assert urllib.request.urlopen(base + "/readyz").status == 200
        body = urllib.request.urlopen(base + "/metrics").read()
        assert b"webcam_ingestion_epoch_total" in body
        assert b"source_stream_id" not in body
    finally:
        server.close()
