/* Test-only native fault tracer. LD_PRELOAD only in explicitly diagnostic runs.
 * Prints addresses/symbols then preserves fatal handling; no model/pixel/policy
 * calls are intercepted. backtrace is not async-signal-safe: this is a best-
 * effort diagnostic, never a production handler or capacity qualification. */
#define _GNU_SOURCE
#include <execinfo.h>
#include <signal.h>
#include <unistd.h>
#include <dlfcn.h>
#include <sys/syscall.h>

static struct sigaction previous_segv, previous_abrt;
static volatile sig_atomic_t entered;
static void (*real_stack_fail)(void);

static void dump(void) {
    void *frames[80];
    const char banner[]="\nSENTRY_TEST_NATIVE_BACKTRACE\n";
    (void)write(STDERR_FILENO,banner,sizeof(banner)-1);
    int count=backtrace(frames,80);
    backtrace_symbols_fd(frames,count,STDERR_FILENO);
}

static void fatal_trace(int signum,siginfo_t *info,void *context) {
    if (!entered) {
        entered=1;
        dump();
    }
    struct sigaction old=signum==SIGSEGV?previous_segv:previous_abrt;
    sigaction(signum,&old,0);
    if (old.sa_handler!=SIG_DFL && old.sa_handler!=SIG_IGN) {
        if (old.sa_flags&SA_SIGINFO) old.sa_sigaction(signum,info,context);
        else old.sa_handler(signum);
    }
    /* An inherited handler may have returned. Preserve fatal semantics. */
    struct sigaction normal={0};normal.sa_handler=SIG_DFL;
    sigemptyset(&normal.sa_mask);sigaction(signum,&normal,0);
    (void)syscall(SYS_tgkill,getpid(),syscall(SYS_gettid),signum);
}

__attribute__((constructor)) static void init_trace(void) {
    void *warmup[1];(void)backtrace(warmup,1);
    real_stack_fail=dlsym(RTLD_NEXT,"__stack_chk_fail");
    struct sigaction action={0};
    action.sa_sigaction=fatal_trace;action.sa_flags=SA_SIGINFO|SA_ONSTACK;
    sigemptyset(&action.sa_mask);
    sigaction(SIGSEGV,&action,&previous_segv);
    sigaction(SIGABRT,&action,&previous_abrt);
}

__attribute__((noreturn)) void __stack_chk_fail(void) {
    if (!entered) {entered=1;dump();}
    if (real_stack_fail) real_stack_fail();
    raise(SIGABRT);
    _exit(134);
}
