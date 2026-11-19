"""Read only the native detection/tracking fields consumed by Sentinel.

The deployed gstgva RegionOfInterest constructor eagerly traverses relation
metadata and constructs related tensor wrappers for every region. Primary
detections do not consume those tensors. Use the same GstAnalytics getters and
first-related tracking rule directly, without constructing unrelated tensors.
No pixels, preprocessing, tracking association, timestamps or thresholds change.
"""


def detections_from_relation(relation, class_names, od_type, track_type,
                             no_relation, quark_to_string):
    from pipeline.types import Detection
    if relation is None:
        return []
    metadata = list(relation)
    tracks = [m for m in metadata if type(m) is track_type]
    labels = {('person' if name == 'pedestrian' else name): key
              for key, name in class_names.items()}
    result = []
    for od in metadata:
        if type(od) is not od_type:
            continue
        quark = od.get_obj_type()
        label = str(quark_to_string(quark) or '') if quark else ''
        class_id = labels.get(label)
        if class_id is None:
            continue
        success, x, y, w, h, _, _ = od.get_oriented_location()
        if not success:
            raise RuntimeError('native detection metadata has no oriented location')
        tracking_id = None
        for track in tracks:
            if track.id == od.id or relation.get_relation(od.id, track.id) == no_relation:
                continue
            success, tracking_id, _, _, _ = track.get_info()
            if not success:
                raise RuntimeError('native detection metadata has invalid tracking info')
            break
        if tracking_id is None:
            raise RuntimeError('gvatrack did not attach a track ID')
        success, confidence = od.get_confidence_lvl()
        if not success:
            raise RuntimeError('native detection metadata has no confidence level')
        box = [int(x), int(y), int(x+w), int(y+h)]
        result.append(Detection(bbox=box, class_id=int(class_id),
            class_name=class_names[class_id], confidence=float(confidence),
            model_name='vehicle', metadata=dict(track_id=int(tracking_id),
                raw_bbox=list(box), predicted=False, tracker_backend='gvatrack')))
    return result


def detections_from_sample(sample, class_names):
    import gi
    gi.require_version('GstAnalytics', '1.0')
    from gi.repository import GstAnalytics, GLib
    # Register the installed DL Streamer custom metadata types before generic
    # relation iteration, exactly as gstgva.VideoFrame does. No ROI is built.
    import gstgva.meta_registry  # noqa:F401
    relation = GstAnalytics.buffer_get_analytics_relation_meta(sample.get_buffer())
    return detections_from_relation(relation, class_names,
        GstAnalytics.ODMtd, GstAnalytics.TrackingMtd, GstAnalytics.RelTypes.NONE,
        GLib.quark_to_string)
