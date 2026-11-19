/* Copy immutable OD/tracking fields through one C boundary. No association,
 * metadata mutation, image access, inference, filtering threshold or policy. */
#include <gst/gst.h>
#include <gst/analytics/analytics.h>
#include <stdint.h>

typedef struct {
    int64_t x1, y1, x2, y2;
    uint64_t track_id;
    double confidence;
    int32_t class_id;
    uint32_t reserved;
} DetectionRow;

unsigned sentinel_detection_abi(void) { return 1; }
unsigned sentinel_detection_row_size(void) { return sizeof(DetectionRow); }

/* Error codes are explicit; the caller must discard the whole partial result.
 * The GstSample owner must keep the buffer alive for the duration of this call. */
int sentinel_detection_read(GstBuffer *buffer, const uint32_t *labels,
                           const int32_t *classes, unsigned label_count,
                           DetectionRow *output, unsigned capacity,
                           unsigned *written) {
    if (!buffer || !written || !output || !labels || !classes) return 1;
    *written = 0;
    if (!label_count || label_count > 256 || capacity > 4096) return 2;
    GstAnalyticsRelationMeta *relation = gst_buffer_get_analytics_relation_meta(buffer);
    if (!relation) return 0;
    GArray *tracks = g_array_new(FALSE, FALSE, sizeof(GstAnalyticsTrackingMtd));
    gpointer state = NULL;
    GstAnalyticsMtd metadata;
    while (gst_analytics_relation_meta_iterate(relation, &state,
               gst_analytics_tracking_mtd_get_mtd_type(), &metadata)) {
        g_array_append_val(tracks, metadata);
    }
    state = NULL;
    int error = 0;
    while (gst_analytics_relation_meta_iterate(relation, &state,
               gst_analytics_od_mtd_get_mtd_type(), &metadata)) {
        GQuark label = gst_analytics_od_mtd_get_obj_type(&metadata);
        unsigned index = 0;
        while (index < label_count && labels[index] != label) ++index;
        if (index == label_count) continue;
        if (*written >= capacity) { error = 3; break; }
        gint x, y, w, h;
        gfloat angle, location_confidence, confidence;
        if (!gst_analytics_od_mtd_get_oriented_location(&metadata, &x, &y, &w, &h,
                                                       &angle, &location_confidence)) {
            error = 4; break;
        }
        gboolean found = FALSE;
        guint64 track_id = 0;
        for (guint i = 0; i < tracks->len; ++i) {
            GstAnalyticsTrackingMtd *track = &g_array_index(tracks, GstAnalyticsTrackingMtd, i);
            if (track->id == metadata.id || gst_analytics_relation_meta_get_relation(
                    relation, metadata.id, track->id) == GST_ANALYTICS_REL_TYPE_NONE) continue;
            GstClockTime first_seen, last_seen;
            gboolean lost;
            found = gst_analytics_tracking_mtd_get_info(track, &track_id, &first_seen,
                                                       &last_seen, &lost);
            break; /* Same FIRST-related-track rule as the frozen GI reader. */
        }
        if (!found) { error = 5; break; }
        if (!gst_analytics_od_mtd_get_confidence_lvl(&metadata, &confidence)) {
            error = 6; break;
        }
        DetectionRow *row = &output[(*written)++];
        row->x1 = x; row->y1 = y;
        row->x2 = (int64_t)x + w; row->y2 = (int64_t)y + h;
        row->track_id = track_id; row->confidence = confidence;
        row->class_id = classes[index]; row->reserved = 0;
    }
    g_array_free(tracks, TRUE);
    return error;
}
