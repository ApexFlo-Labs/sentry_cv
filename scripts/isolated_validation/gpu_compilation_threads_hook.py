"""Diagnostic only: set one supported GPU compilation thread before PLAYING.

Do not change nireq, batch, streams, inference precision, pixels or event gates.
Injected through the explicit harness hook, not part of the frozen CV image.
"""
import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst
import json
import sys

# PyGObject exposes pre-init fake methods; capture the real parsing functions
# only after initialization. This is test setup, before any graph is created.
Gst.init([])

def configure(graph):
    iterator=graph.iterate_recurse()
    while True:
        result,element=iterator.next()
        if result==Gst.IteratorResult.DONE:
            break
        if result==Gst.IteratorResult.RESYNC:
            iterator.resync()
            continue
        if result!=Gst.IteratorResult.OK:
            raise RuntimeError('Could not enumerate diagnostic GVA graph')
        factory=element.get_factory()
        if factory is None or factory.get_name() not in ['gvadetect','gvaclassify','gvainference']:
            continue
        device=str(element.get_property('device'))
        if 'GPU' not in device.upper():
            continue
        original=str(element.get_property('ie-config') or '')
        if 'COMPILATION_NUM_THREADS' in original:
            raise RuntimeError('Compiler diagnostic must not overwrite an existing value')
        updated=original+(',' if original else '')+'COMPILATION_NUM_THREADS=1'
        element.set_property('ie-config',updated)
        assert element.get_property('ie-config')==updated
        print('SENTRY_TEST_COMPILATION_THREADS '+json.dumps({
            'element':element.get_name(),'factory':factory.get_name(),'device':device,
            'compile_threads':1,'prior_ie_config':original,
            'scope':'Diagnostic compilation only, no policy/inference geometry changes'}),
            file=sys.stderr,flush=True)
    return graph


_parse_bin=Gst.parse_bin_from_description
_parse_launch=Gst.parse_launch


def parse_bin(*args,**kwargs):
    return configure(_parse_bin(*args,**kwargs))


def parse_launch(*args,**kwargs):
    graph=_parse_launch(*args,**kwargs)
    return configure(graph) if isinstance(graph,Gst.Bin) else graph


Gst.parse_bin_from_description=parse_bin
Gst.parse_launch=parse_launch
