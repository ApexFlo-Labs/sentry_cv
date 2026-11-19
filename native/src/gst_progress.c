/* Read-only pad progress probes. No buffer mutation, mapping, drop or policy. */
#include <gst/gst.h>
#include <glib.h>
#include <stdint.h>

#define STAGES 64
typedef struct { uint64_t count, last_ns, pts; } Entry;
typedef struct {
    gatomicrefcount refs;
    GMutex lock;
    gboolean closed;
    Entry entries[STAGES];
} State;
typedef struct { State *state; guint index; } ProbeData;
typedef struct { GstPad *pad; gulong id; } Probe;
typedef struct { State *state; GArray *probes; } Handle;

static void state_unref(State *s) {
    if (g_atomic_ref_count_dec(&s->refs)) {
        g_mutex_clear(&s->lock);
        g_free(s);
    }
}

static void destroy_probe(gpointer value) {
    ProbeData *d = value;
    state_unref(d->state);
    g_free(d);
}

static GstPadProbeReturn observe(GstPad *pad, GstPadProbeInfo *info, gpointer value) {
    (void)pad;
    GstBuffer *buffer = GST_PAD_PROBE_INFO_BUFFER(info);
    if (buffer != NULL) {
        ProbeData *d = value;
        State *s = d->state;
        g_mutex_lock(&s->lock);
        if (!s->closed) {
            Entry *e = &s->entries[d->index];
            if (e->count != UINT64_MAX) ++e->count;
            e->last_ns = (uint64_t)g_get_monotonic_time() * 1000;
            e->pts = GST_BUFFER_PTS(buffer);
        }
        g_mutex_unlock(&s->lock);
    }
    return GST_PAD_PROBE_OK;
}

unsigned sentinel_progress_abi(void) { return 1; }
unsigned sentinel_progress_entry_size(void) { return sizeof(Entry); }

void *sentinel_progress_new(void) {
    Handle *h = g_try_new0(Handle, 1);
    State *s = g_try_new0(State, 1);
    if (!h || !s) { g_free(h); g_free(s); return NULL; }
    g_atomic_ref_count_init(&s->refs);
    g_mutex_init(&s->lock);
    h->state = s;
    h->probes = g_array_new(FALSE, FALSE, sizeof(Probe));
    return h;
}

uint64_t sentinel_progress_attach(void *handle, GstPad *pad, unsigned index) {
    Handle *h = handle;
    if (!h || !pad || index >= STAGES || !GST_IS_PAD(pad)) return 0;
    g_mutex_lock(&h->state->lock);
    gboolean closed = h->state->closed;
    g_mutex_unlock(&h->state->lock);
    if (closed) return 0;
    ProbeData *d = g_try_new0(ProbeData, 1);
    if (!d) return 0;
    d->state = h->state;
    d->index = index;
    g_atomic_ref_count_inc(&d->state->refs);
    Probe p = { gst_object_ref(pad), 0 };
    p.id = gst_pad_add_probe(p.pad, GST_PAD_PROBE_TYPE_BUFFER, observe, d, destroy_probe);
    if (p.id == 0) {
        /* BUFFER probes returning OK never remove themselves during install.
         * A valid pad is checked above. Report an impossible installation
         * failure without using any potentially destroyed callback data. */
        gst_object_unref(p.pad);
        return 0;
    }
    g_array_append_val(h->probes, p);
    return p.id;
}

unsigned sentinel_progress_snapshot(void *handle, Entry *out, unsigned capacity) {
    Handle *h = handle;
    if (!h || !out) return 0;
    unsigned n = capacity < STAGES ? capacity : STAGES;
    g_mutex_lock(&h->state->lock);
    for (unsigned i = 0; i < n; ++i) out[i] = h->state->entries[i];
    g_mutex_unlock(&h->state->lock);
    return n;
}

void sentinel_progress_stop(void *handle) {
    Handle *h = handle;
    if (!h) return;
    g_mutex_lock(&h->state->lock);
    h->state->closed = TRUE;
    g_mutex_unlock(&h->state->lock);
    for (guint i = 0; i < h->probes->len; ++i) {
        Probe *p = &g_array_index(h->probes, Probe, i);
        gst_pad_remove_probe(p->pad, p->id);
        gst_object_unref(p->pad);
    }
    g_array_set_size(h->probes, 0);
}

void sentinel_progress_free(void *handle) {
    Handle *h = handle;
    if (!h) return;
    sentinel_progress_stop(h);
    g_array_free(h->probes, TRUE);
    state_unref(h->state);
    g_free(h);
}
