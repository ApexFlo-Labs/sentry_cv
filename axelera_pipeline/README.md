# Axelera pipeline (work in progress, paused)

Runs our traffic-v11 detector on the Axelera Metis card. **Not ready to replace the FP16 detector:** INT8 boxes still differ too much. Paused until the fine-tuned INT8 models arrive; the Intel DL Streamer pipeline is the current priority.

Tested on the Beige box (`compute-a`): Core Ultra 9 285H, Metis M.2 (1 GiB, 4 cores), Voyager SDK 1.6.0. Only the detector runs on Metis so far. There is no mixed pipeline yet.

## Results

Low box load (about 2-4), `fakevideo@1000`, 4 AIPU cores.

| Model | End-to-end | Card time per frame |
|---|---|---|
| yolov8n-coco (Axelera) | 715 fps | 1.39 ms |
| yolo26n-coco-onnx (Axelera) | 482 fps | 2.04 ms |
| traffic-v11-ours | 522 fps | 1.88 ms |
| traffic-v11-clip | 517 fps | 1.90 ms |

`fakevideo` has no video decode, so these are not camera-capacity numbers.

Boxes against our FP16 detector on the iGPU: 300 frames per clip (`cars` has 100), confidence >= 0.30, same class and IoU >= 0.5. There are no labels, so this shows how far INT8 moved from FP16, not which one is right. The two sides also resize frames differently, and that effect has not been separated.

| Clip | Unmatched, ours | Unmatched, clip | Person, ours | Person, clip |
|---|---|---|---|---|
| junction | 10.9% | 10.8% | +13.4% | +13.4% |
| cars | 16.6% | 14.4% | +58% | +62% |
| crowd | 17.1% | 15.6% | +7.9% | -0.8% |
| hospital | 34.9% | 30.7% | +34.4% | +6.3% |

67-88% of unmatched boxes are below confidence 0.40. The clip model is worse for hospital motorcycles (-24.4%, was -10.2%). Hospital trucks go from 75 to 0 in both; FP16 scored those low (0.30-0.46).

## Problems found and fixed

1. **Quantization failed at the attention Softmax** (`External op ... Dequantize`). Our ONNX was opset 12. Axelera official YOLO26n and our model converted to opset 17 both pass. The converted file also has the attention `Mul` moved and Softmax `axis=-1`; opset 17 was not tested without those two. Boxes are identical to the original ONNX on the 2 clips tested, both mostly people, so vehicles are barely covered by this check.
2. **SDK decoder bug** (`invalid box tensor shape ... got [80, 80, 9]`). `AxDecodeYolo8.cpp` assumes 64 box channels, so a YOLO26 model with 4 box channels and fewer than 64 classes has its box and class tensors swapped. See `decoder/AxDecodeYolo8_swap_fix.patch`.
3. **Coarse scores.** Min-max quantization gave class tensors a range of 48-89, so scores came in large steps. `per_tensor_histogram` fails on the attention `MatMul`. Clipping class outputs to [-8, 8] (`tools/clip_logits.py`) cut the range to about 11-12 at no speed cost, but only modestly reduced the differences.

## Open

- INT8 accuracy, including how much comes from resize differences or calibration frames.
- Several models on the card together; only one has been run at a time.
- `inference.py -o` sometimes segfaults; a retry has worked so far.
- Paths are hard-coded for this box (`/home/admin1/chandana_axelera`, `/home/admin1/voyager-sdk-latest`).

## Rebuild

Model files are not committed. The source ONNX is `models/vehicle_yolo26n.onnx` in `ApexFlo-Labs/UnAuthorized-Parking` (md5 `7f71d9378aeb5173b6865857560310aa`).

```bash
python tools/move_attn_mul.py vehicle_yolo26n.onnx vehicle_yolo26n_mulmoved.onnx
python tools/make_variants.py vehicle_yolo26n_mulmoved.onnx v_axis.onnx vehicle_yolo26n_opset17.onnx
python tools/clip_logits.py vehicle_yolo26n_opset17.onnx vehicle_yolo26n_clip8.onnx 8
```

The decoder is built from a copy of the SDK `operators` and `trackers` folders with the patch applied (`cmake --build ... --target decode_yolov8`). `decoder/yolo10_ours.py` points at that library by full path, because Voyager does not use `LD_LIBRARY_PATH` for it.

`deploy.py` only finds YAMLs inside the SDK folder, so copy them to `$AXELERA_FRAMEWORK/customers/<name>/` and adjust their paths. Calibration frames go in `<data-root>/traffic_v11/repr_imgs`. `val_data` is a placeholder because SDK 1.6 requires the key and we have no labelled set.

```bash
deploy.py traffic-v11-clip --mode QUANTCOMPILE --metis m2 --build-root <dir> --data-root <dir> --num-cal-images 277
```

To stop early on quantization problems, use `--mode QUANTIZE --num-cal-images 2`, which fails in under a minute instead of hours.
