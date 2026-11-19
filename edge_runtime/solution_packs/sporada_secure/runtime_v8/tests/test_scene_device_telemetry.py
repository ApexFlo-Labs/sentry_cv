"""Report executor placement and queue/model spans without invented GPU work."""
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/worker'))
from pipeline.v8_events import V8EventPipeline
from pipeline.runtime_telemetry import CameraTelemetry


def test_scene_executor_uses_actual_device_and_keeps_queue_separate():
 for device in ['NPU/DLStreamer','GPU/DLStreamer',None]:
  records=[];p=V8EventPipeline.__new__(V8EventPipeline)
  p.telemetry=SimpleNamespace(observe_latency=lambda *a,**k:records.append((a,k)))
  p.scene=SimpleNamespace(scheduler_queue_wait_seconds=.8,scheduler_device_inference_seconds=.02,actual_device=device)
  p._observe_shared_scene_execution()
  assert records==[(('scene_search.scheduler_queue_wait',.8),{}),(('scene_search.device_inference',.02),{'device':device})]


def test_scene_total_is_a_parent_span_not_an_extra_device_model_call():
 for stage in ['scene_search.embedding','object_presence.semantic_embedding']:
  assert CameraTelemetry._stage_semantics(stage)==('pipeline_span_wall_time',True)
 assert CameraTelemetry._stage_semantics('scene_search.device_inference')==('model_invocation_wall_time',False)
 assert CameraTelemetry._stage_semantics('scene_search.scheduler_queue_wait')==('pipeline_stage_wall_time',False)
