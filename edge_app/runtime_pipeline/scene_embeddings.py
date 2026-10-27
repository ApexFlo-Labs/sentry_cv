"""OpenVINO SigLIP scene embedding extractor."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

SCENE_MODEL_ID = "google/siglip2-base-patch16-224"
SCENE_MODEL_VERSION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
SCENE_DIMENSIONS = 768
DEFAULT_EMBEDDING_SPACE = (
    "google/siglip2-base-patch16-224@75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2:image-text:l2:768"
)


class OpenVINOSceneExtractor:
    model_id = SCENE_MODEL_ID
    model_version = SCENE_MODEL_VERSION
    dimension = SCENE_DIMENSIONS

    def __init__(self, model_path: str, device: str = "GPU") -> None:
        try:
            import openvino as ov
            from openvino import Layout, Type
            from openvino.preprocess import ColorFormat, PrePostProcessor
        except ImportError as exc:
            raise RuntimeError("OpenVINO is required for scene embeddings") from exc
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"scene embedding model is missing: {path}")
        core = ov.Core()
        model = core.read_model(str(path))
        if len(model.inputs) != 1:
            raise ValueError("scene image encoder must expose exactly one image input")
        # The exported SigLIP IR has fully dynamic input dimensions.  NPU
        # compilation needs static spatial dimensions (in particular for the
        # model's position-embedding Tile operation), and embed() always
        # supplies a single 224x224 image.
        model.reshape({model.input(0): [1, 3, 224, 224]})
        # Resize remains explicit because callers may submit arbitrary zone
        # shapes.  Color conversion, type conversion, normalization and layout
        # conversion are graph operations and therefore execute with the GPU
        # model instead of allocating multiple CPU-side image tensors.
        ppp = PrePostProcessor(model)
        inp = ppp.input()
        inp.tensor().set_element_type(Type.u8).set_layout(Layout("NHWC")).set_color_format(ColorFormat.BGR)
        inp.model().set_layout(Layout("NCHW"))
        inp.preprocess().convert_element_type(Type.f32).convert_color(ColorFormat.RGB).mean(127.5).scale(127.5)
        model = ppp.build()
        self.compiled = core.compile_model(model, device)
        self.input = self.compiled.input(0)
        self.output = self.compiled.output(0)
        output_shape = self.output.partial_shape
        static_last_dimension = (
            output_shape.rank.is_static
            and len(output_shape) > 0
            and output_shape[-1].is_static
        )
        if static_last_dimension and output_shape[-1].get_length() != self.dimension:
            raise ValueError(
                f"scene model output must end in {self.dimension}, got {output_shape}"
            )

    def warmup(self) -> None:
        self.embed(np.zeros((224, 224, 3), dtype=np.uint8))

    def embed(self, image: np.ndarray) -> np.ndarray:
        if image is None or image.size == 0:
            raise ValueError("scene image is empty")
        resized = cv2.resize(image, (224, 224), interpolation=cv2.INTER_AREA)
        # The graph's (x - 127.5) / 127.5 preprocessing preserves SigLIP's
        # exact [0, 255] -> [-1, 1] mapping.
        result = np.asarray(self.compiled([resized[None]])[self.output], dtype=np.float32).reshape(-1)
        if result.size != self.dimension or not np.isfinite(result).all():
            raise ValueError("scene model returned an invalid vector")
        norm = float(np.linalg.norm(result))
        if norm <= 1e-12:
            raise ValueError("scene model returned a zero vector")
        return result / norm

