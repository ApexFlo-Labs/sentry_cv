/* Two-process image path, parent side (test): every Nth frame on the pad, export the frame's GPU surface as a DMABuf
   (vaExportSurfaceHandle, read-only, composed layers) and send the descriptor + file descriptors over a Unix
   SOCK_SEQPACKET socket to the image process. No pixels are copied. The buffer is kept (ring of HOLD frames) so the
   surface stays valid while the child uses it. Built without libva/GStreamer headers (stable ABI declared here). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>

typedef struct { int type; unsigned long id; void *data; } ProbeInfo;
typedef int (*ProbeFunc)(void *pad, ProbeInfo *info, void *data);
extern unsigned long gst_pad_add_probe(void *pad, int mask, ProbeFunc func, void *data, void *destroy);
extern void *gst_mini_object_ref(void *o);
extern void gst_mini_object_unref(void *o);
extern unsigned int gst_va_buffer_get_surface(void *buffer);
extern void *gst_va_buffer_peek_display(void *buffer);
extern void *gst_va_display_get_va_dpy(void *display);
extern int vaExportSurfaceHandle(void *dpy, unsigned int surface, uint32_t mem_type, uint32_t flags, void *desc);
extern int vaSyncSurface(void *dpy, unsigned int surface);

typedef struct {
    uint32_t fourcc, width, height, num_objects;
    struct { int fd; uint32_t size; uint64_t drm_format_modifier; } objects[4];
    uint32_t num_layers;
    struct { uint32_t drm_format, num_planes, object_index[4], offset[4], pitch[4]; } layers[4];
} VADesc;
typedef struct {  /* message to the image process (fds travel as SCM_RIGHTS) */
    int64_t seq, pts; uint32_t width, height, fourcc, num_objects, sizes[4]; uint64_t modifiers[4];
    uint32_t drm_format, num_planes, object_index[4], offset[4], pitch[4]; int32_t cam;
} Msg;
#define HOLD 16
typedef struct { int sock, cam, every; long n, seq; char path[108]; void *held[HOLD]; long w; long sent, fail_export, fail_send; } Ctx;
static inline long long buf_pts(void *b) { return *(long long *)((unsigned char *)b + 72); }

static int ensure_socket(Ctx *c) {
    if (c->sock >= 0) return 1;
    int s = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    struct sockaddr_un a; memset(&a, 0, sizeof a); a.sun_family = AF_UNIX; strncpy(a.sun_path, c->path, sizeof a.sun_path - 1);
    if (s < 0 || connect(s, (struct sockaddr *)&a, sizeof a) != 0) { if (s >= 0) close(s); return 0; }
    c->sock = s; return 1;
}

static int probe(void *pad, ProbeInfo *info, void *data) {
    Ctx *c = data;
    void *buf = info->data;
    if (c->n++ % c->every) return 1;
    unsigned int surf = gst_va_buffer_get_surface(buf);
    void *disp = gst_va_buffer_peek_display(buf);
    if (surf == 0xffffffffu || !disp) { c->fail_export++; return 1; }
    void *dpy = gst_va_display_get_va_dpy(disp);
    VADesc d; memset(&d, 0, sizeof d);
    vaSyncSurface(dpy, surf);
    if (vaExportSurfaceHandle(dpy, surf, 0x40000000 /* DRM_PRIME_2 */, 0x0001 | 0x0008 /* READ_ONLY | COMPOSED_LAYERS */, &d) != 0) { c->fail_export++; return 1; }
    Msg m; memset(&m, 0, sizeof m);
    m.seq = ++c->seq; m.pts = buf_pts(buf); m.width = d.width; m.height = d.height; m.fourcc = d.fourcc; m.num_objects = d.num_objects; m.cam = c->cam;
    for (unsigned i = 0; i < d.num_objects && i < 4; i++) { m.sizes[i] = d.objects[i].size; m.modifiers[i] = d.objects[i].drm_format_modifier; }
    m.drm_format = d.layers[0].drm_format; m.num_planes = d.layers[0].num_planes;
    for (int p = 0; p < 4; p++) { m.object_index[p] = d.layers[0].object_index[p]; m.offset[p] = d.layers[0].offset[p]; m.pitch[p] = d.layers[0].pitch[p]; }
    int ok = 0;
    if (ensure_socket(c)) {
        char cbuf[CMSG_SPACE(sizeof(int) * 4)]; memset(cbuf, 0, sizeof cbuf);
        struct iovec iov = {&m, sizeof m};
        struct msghdr h; memset(&h, 0, sizeof h); h.msg_iov = &iov; h.msg_iovlen = 1; h.msg_control = cbuf; h.msg_controllen = CMSG_SPACE(sizeof(int) * d.num_objects);
        struct cmsghdr *cm = CMSG_FIRSTHDR(&h); cm->cmsg_level = SOL_SOCKET; cm->cmsg_type = SCM_RIGHTS; cm->cmsg_len = CMSG_LEN(sizeof(int) * d.num_objects);
        for (unsigned i = 0; i < d.num_objects; i++) ((int *)CMSG_DATA(cm))[i] = d.objects[i].fd;
        ok = sendmsg(c->sock, &h, MSG_DONTWAIT | MSG_NOSIGNAL) == (ssize_t)sizeof m;
        if (!ok) { close(c->sock); c->sock = -1; }
    }
    for (unsigned i = 0; i < d.num_objects; i++) close(d.objects[i].fd);  /* the receiver holds its own copies */
    if (!ok) { c->fail_send++; return 1; }
    c->sent++;
    gst_mini_object_ref(buf);  /* keep the surface alive while the image process uses it */
    long i = c->w++ % HOLD;
    if (c->held[i]) gst_mini_object_unref(c->held[i]);
    c->held[i] = buf;
    return 1;
}

void *cexport_attach(void *pad, const char *socket_path, int cam, int every) {
    Ctx *c = calloc(1, sizeof(Ctx));
    c->sock = -1; c->cam = cam; c->every = every > 0 ? every : 1;
    strncpy(c->path, socket_path, sizeof c->path - 1);
    gst_pad_add_probe(pad, 1 << 4, probe, c, 0);
    return c;
}
void cexport_counts(void *cv, long *out) { Ctx *c = cv; out[0] = c->sent; out[1] = c->fail_export; out[2] = c->fail_send; }
