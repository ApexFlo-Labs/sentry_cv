"""Static copy of the 9-class traffic-v11 detector for DL Streamer (it is saved with a dynamic input).

    prepare_v11.py SOURCE.xml OUT.xml

Reads the model (the source is only read), fixes the input to [1,3,640,640] and saves it in our folder.
Prints the input/output shapes and the class names stored in the model.
"""
import re
import sys

import openvino as ov

src, out = sys.argv[1:3]
model = ov.Core().read_model(src)
print("source input", model.input(0).get_partial_shape(), "outputs", [str(o.get_partial_shape()) for o in model.outputs])
model.reshape({model.input(0): [1, 3, 640, 640]})
ov.save_model(model, out, compress_to_fp16=False)
names = re.findall(r'<names value="([^"]*)', open(out, errors="ignore").read())
print("saved", out, "| input", model.input(0).get_partial_shape(), "| outputs",
      [str(o.get_partial_shape()) for o in model.outputs], "| names", names[:1] or "none")
