/* Removes the mux batch meta before gvametaconvert, which otherwise writes only "streams" and no boxes. Runs per frame in C, so it does not take the Python lock.
   The box has no GStreamer headers, so the few stable declarations needed are written here. */
#include <string.h>

typedef unsigned long GType;
typedef struct { GType api; } MetaInfo;
typedef struct { unsigned int flags; const MetaInfo *info; } Meta;
typedef struct { int type; unsigned long id; void *data; } ProbeInfo;
typedef int (*MetaFunc)(void *buffer, Meta **meta, void *data);
typedef int (*ProbeFunc)(void *pad, ProbeInfo *info, void *data);

extern const char *g_type_name(GType type);
extern void *gst_mini_object_make_writable(void *object);
extern int gst_buffer_foreach_meta(void *buffer, MetaFunc func, void *data);
extern unsigned long gst_pad_add_probe(void *pad, int mask, ProbeFunc func, void *data, void *destroy);

#define PROBE_TYPE_BUFFER (1 << 4)
#define PROBE_OK 1

static int drop(void *buffer, Meta **meta, void *data) {
    const char *name = g_type_name((*meta)->info->api);
    if (name && strstr(name, "BatchMeta")) {
        *meta = 0;
    }
    return 1;
}

static int probe(void *pad, ProbeInfo *info, void *data) {
    void *buffer = gst_mini_object_make_writable(info->data);
    gst_buffer_foreach_meta(buffer, drop, 0);
    info->data = buffer;
    return PROBE_OK;
}

void strip_batch_meta(void *pad) {
    gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, probe, 0, 0);
}
