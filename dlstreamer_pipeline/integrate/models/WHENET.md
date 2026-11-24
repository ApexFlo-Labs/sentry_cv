# whenet_fp16.xml / .bin - head pose model (replaces best_a0_fp16 in the contract image, user decision 2026-11-24)

Source: PINTO0309 model zoo, `whenet_1x3x224x224_prepost.onnx` (WHENet, BSD-3 licence),
sha256 28345f49bd38a3e74f5d6b028469fe27b6bf5b09405ffdfa0be4b28db6c2be0e; converted with OpenVINO convert_model +
compress_to_fp16 (face-pipeline session, ~/Documents/dls-face-trace-2026-11-20/cluster/models/).
- whenet_fp16.xml sha256 d41ce2641d291a86bd92e191aa5cb927c602bfa2c2aab932eb4483a9df6b92f0
- whenet_fp16.bin sha256 465870c7591c825f26e28c4ee34f09174ccf51c11c345a27e4ce01baa2da91f2

Input [1,3,224,224]: face box widened x +-20 %, y +-10 % (clamped), stretched to 224x224, RGB 0..255 float (the
normalisation is inside the model). Output [1,3]: yaw, roll, pitch in degrees. NPU ~2.5 ms per face.
Measured on the site identity crops (face-pipeline session): catches looking-down faces the SixDRepNet A0 model
passes; gives wrong large yaw on some clear frontal faces. INT8 and smaller inputs were tested and rejected.
