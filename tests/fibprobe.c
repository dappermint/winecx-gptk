/* What is in NtTib.FiberData before anything becomes a fiber?
 *
 * wine's ConvertThreadToFiberEx refuses with ERROR_ALREADY_FIBER whenever that
 * field is non-zero (dlls/kernelbase/thread.c). DOOM dies switching to 0x8ff,
 * which is exactly what its TEB slot held, so the whole question is who put a
 * non-pointer there: wine at thread setup, or the game.
 *
 * Writes to a file because a second wine attached to an existing wineserver has
 * nowhere to print.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o fibprobe.exe fibprobe.c
 */
#include <windows.h>
#include <stdio.h>

static FILE *out;

/* what GetCurrentFiber() compiles to: __readgsqword(0x20) */
static void *gs_at(int off)
{
    void *v;
    switch (off) {
    case 0x00: __asm__ volatile ("movq %%gs:0x00, %0" : "=r" (v)); break;
    case 0x08: __asm__ volatile ("movq %%gs:0x08, %0" : "=r" (v)); break;
    case 0x10: __asm__ volatile ("movq %%gs:0x10, %0" : "=r" (v)); break;
    case 0x18: __asm__ volatile ("movq %%gs:0x18, %0" : "=r" (v)); break;
    case 0x20: __asm__ volatile ("movq %%gs:0x20, %0" : "=r" (v)); break;
    case 0x28: __asm__ volatile ("movq %%gs:0x28, %0" : "=r" (v)); break;
    case 0x30: __asm__ volatile ("movq %%gs:0x30, %0" : "=r" (v)); break;
    default: v = NULL;
    }
    return v;
}

/* The claim to prove or kill: is the gs segment the TEB, or something else with
 * only the Self slot made to line up? Read the fiber pointer both ways in one
 * place -- through gs directly, and by chasing gs:0x30 as a TEB pointer. On
 * Windows these are the same 8 bytes by definition. */
static void report(const char *who)
{
    void *teb = gs_at(0x30);
    void *via_gs = gs_at(0x20);
    void *via_teb = teb ? *(void **)((char *)teb + 0x20) : NULL;

    fprintf(out, "%-22s tid %-6lu  gs:0x20 = %-18p  TEB+0x20 = %-18p  %s\n",
            who, GetCurrentThreadId(), via_gs, via_teb,
            via_gs == via_teb ? "same" : "*** DIFFERENT ***");
    fflush(out);
}

static DWORD WINAPI worker(void *arg)
{
    report((const char *)arg);

    void *f = ConvertThreadToFiber(NULL);
    fprintf(out, "  ConvertThreadToFiber -> %p%s (err %lu), gs:0x20 now %p\n",
            f, f ? "" : "  <== FAILED", GetLastError(), gs_at(0x20));
    fflush(out);
    return 0;
}

int main(int argc, char **argv)
{
    out = fopen(argc > 1 ? argv[1] : "fibprobe.txt", "w");
    if (!out) return 1;

    report("main thread");

    HANDLE h[3];
    static const char *names[] = { "fresh thread 1", "fresh thread 2", "fresh thread 3" };
    for (int i = 0; i < 3; i++)
        h[i] = CreateThread(NULL, 0, worker, (void *)names[i], 0, NULL);
    WaitForMultipleObjects(3, h, TRUE, 10000);

    /* and on the main thread, which is what a game converts first */
    void *f = ConvertThreadToFiber(NULL);
    fprintf(out, "main ConvertThreadToFiber -> %p%s (err %lu), gs:0x20 now %p\n",
            f, f ? "" : "  <== FAILED", GetLastError(), gs_at(0x20));

    report("  main after convert");
    fprintf(out, "\nwindows puts the NT_TIB Version union member here; a non-zero\n"
                 "value makes wine's ConvertThreadToFiberEx return ERROR_ALREADY_FIBER\n");
    fclose(out);
    return 0;
}
