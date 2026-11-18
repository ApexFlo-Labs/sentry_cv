/* Test only: record-only probes for the accuracy check. They use the same box reading and the same decisions as
   the live gates (cgates.c is included), write "frame boxes counted removed_ids" per frame to a file, and remove
   nothing, so a Python recorder after them sees the identical boxes. */
#include <stdio.h>
#include <stdlib.h>
#include "cgates.c"

typedef struct { FILE *f; long frame; int face; } Rec;

static int record_probe(void *pad, ProbeInfo *info, void *data) {
    Rec *r = data;
    Box b[MAXB];
    char drop[MAXB];
    int n = read_boxes(info->data, b);
    int counted = r->face ? decide_face(b, n, drop) : decide_dedup(b, n, drop);
    fprintf(r->f, "%ld %d %d", r->frame++, n, counted);
    int first = 1;
    for (int i = 0; i < n; i++)
        if (drop[i]) { fprintf(r->f, "%s%u", first ? " " : ",", b[i].id); first = 0; }
    fprintf(r->f, "\n");
    return PROBE_OK;
}

void cgates_record(void *pad, const char *path, int face) {
    Rec *r = calloc(1, sizeof(Rec));
    r->f = fopen(path, "w");
    r->face = face;
    gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, record_probe, r, 0);
}
void cgates_record_close(void) { fflush(0); }
