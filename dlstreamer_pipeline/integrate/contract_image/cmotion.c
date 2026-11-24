
/* ---------------- T2 parity D: motion gate in C (appended to a COPY of cgates.c by t2_parity_patch.py) ----------------
   T2's design (sentry_cv v8-t2-fix2f surface_stream.SurfaceSwitch + dlstreamer_bridge.DlsMotionPolicyGate), the same
   numbers, as pad probes in C like her other per-frame steps (no Python in the streaming threads: a Python probe per
   frame froze the whole engine in a test on 2026-11-24).
     gate probe (identity mg<n>, after gvamotiondetect, before the shared detector): the frame's "motion" regions are
       the vote. Active camera: every frame passes. Quiet camera: a frame passes when motion was seen in >= 4 of the last
       14 frames (-> active again) or 5 s passed since the last check (safety sweep); all other frames are DROPPED here
       (no inference, tracking, faces, plates, pixels) and counted as quiet frames.
     detector probe (identity vd<n>, after the detector): any box -> stays/becomes active; 6 admitted frames in a row
       with no box and no recent fire/smoke box -> quiet.
     fire probe (fdet<n>, the 2 frames/s fire branch, never gated): a fire/smoke box keeps or makes the camera active.
   Time is CLOCK_MONOTONIC (T2 used source PTS; those are not running time for tcp:// or rebuilt sources). */
#define MG_WIN 14
#define MG_NEED 4
#define MG_RELEASE 6
#define MG_SWEEP_S 5.0
typedef struct {
    int tracking, miss, hist[MG_WIN], hn, hpos, lock, fire_hit;
    double last_sweep, fire_at;
    long passed, quiet, motion, sweeps, fire_hits, det_hits;
} MGate;

static double mg_now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static inline void mg_lock(MGate *g) { while (__atomic_exchange_n(&g->lock, 1, __ATOMIC_ACQUIRE)) ; }
static inline void mg_unlock(MGate *g) { __atomic_store_n(&g->lock, 0, __ATOMIC_RELEASE); }

/* boxes on the buffer: want != 0 -> count boxes labelled `label`; want == 0 -> count boxes NOT labelled `label` */
static int mg_count(void *buffer, const char *label, int want) {
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    if (!rm) return 0;
    gpointer state = 0; Mtd mtd; int n = 0;
    GType od = gst_analytics_od_mtd_get_mtd_type();
    while (gst_analytics_relation_meta_iterate(rm, &state, od, &mtd)) {
        const char *l = g_quark_to_string(gst_analytics_od_mtd_get_obj_type(&mtd));
        int same = l && strcmp(l, label) == 0;
        if (same == (want != 0)) n++;
    }
    return n;
}

static void mg_observe(MGate *g, int detected) {  /* SurfaceSwitch.observe */
    if (!g->tracking) {
        if (detected) { g->tracking = 1; g->miss = 0; }
        return;
    }
    if (detected) { g->miss = 0; return; }
    if (++g->miss >= MG_RELEASE) { g->tracking = 0; g->miss = 0; g->hn = g->hpos = 0; memset(g->hist, 0, sizeof g->hist); }
}

static void mg_observe_models(MGate *g, int vehicle_hit, int smoke_hit, int fully_evaluated) {
    if (vehicle_hit || smoke_hit) mg_observe(g, 1);
    else if (fully_evaluated) mg_observe(g, 0);
}

static int mg_gate_probe(void *pad, ProbeInfo *info, void *data) {
    MGate *g = data;
    int candidate = mg_count(info->data, "motion", 1) > 0;
    double now = mg_now();
    int pass = 1;
    mg_lock(g);
    g->motion += candidate;
    if (g->last_sweep < 0) g->last_sweep = now;
    if (!g->tracking) {  /* SurfaceSwitch.update while quiet */
        g->hist[g->hpos] = candidate; g->hpos = (g->hpos + 1) % MG_WIN; if (g->hn < MG_WIN) g->hn++;
        int sum = 0; for (int i = 0; i < g->hn; i++) sum += g->hist[i];
        if (sum >= MG_NEED) { g->tracking = 1; g->miss = 0; }
        else if (now - g->last_sweep >= MG_SWEEP_S) { g->last_sweep = now; g->sweeps++; }
        else pass = 0;
    } else {
        g->last_sweep = now;
    }
    if (pass) g->passed++; else g->quiet++;
    mg_unlock(g);
    return pass ? PROBE_OK : 0;  /* 0 = GST_PAD_PROBE_DROP */
}

static int mg_det_probe(void *pad, ProbeInfo *info, void *data) {
    MGate *g = data;
    int hit = mg_count(info->data, "motion", 0) > 0;
    double now = mg_now();
    mg_lock(g);
    g->det_hits += hit;
    int fire_recent = now - g->fire_at <= 1.0;
    mg_observe_models(g, hit, fire_recent && g->fire_hit, fire_recent);
    mg_unlock(g);
    return PROBE_OK;
}

static int mg_fire_probe(void *pad, ProbeInfo *info, void *data) {
    MGate *g = data;
    int hit = mg_count(info->data, "motion", 0) > 0;
    mg_lock(g);
    g->fire_at = mg_now(); g->fire_hit = hit; g->fire_hits += hit;
    if (hit) mg_observe_models(g, 0, 1, 1);
    mg_unlock(g);
    return PROBE_OK;
}

void *cmotion_new(void) {
    MGate *g = calloc(1, sizeof(MGate));
    g->tracking = 1; g->last_sweep = -1; g->fire_at = -1e9;
    return g;
}
void cmotion_attach(void *g, void *gate_pad, void *det_pad, void *fire_pad) {
    gst_pad_add_probe(gate_pad, PROBE_TYPE_BUFFER, mg_gate_probe, g, 0);
    gst_pad_add_probe(det_pad, PROBE_TYPE_BUFFER, mg_det_probe, g, 0);
    gst_pad_add_probe(fire_pad, PROBE_TYPE_BUFFER, mg_fire_probe, g, 0);
}
void cmotion_reset(void *gp) {  /* a reused idle branch gets a new camera: start active, empty history */
    MGate *g = gp;
    mg_lock(g);
    g->tracking = 1; g->miss = 0; g->hn = g->hpos = 0; memset(g->hist, 0, sizeof g->hist); g->last_sweep = -1;
    mg_unlock(g);
}
void cmotion_stats(void *gp, long *out) {  /* tracking, passed, quiet, motion, sweeps, fire_hits, det_hits */
    MGate *g = gp;
    mg_lock(g);
    out[0] = g->tracking; out[1] = g->passed; out[2] = g->quiet; out[3] = g->motion;
    out[4] = g->sweeps; out[5] = g->fire_hits; out[6] = g->det_hits;
    mg_unlock(g);
}
