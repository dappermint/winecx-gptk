/* Can the built runtime open a window?
 *
 * A driver whose init fails degrades to nodrv silently: wine keeps running,
 * `wine --version` and `wineboot -u` both succeed, and the only symptom is
 * that top-level windows stop existing.  A patch of ours shipped in that
 * state and broke every GUI app, chromium included, before anyone noticed.
 *
 * Message-only windows never go through the display driver, so creating both
 * kinds separates "the driver is gone" from "windows are broken generally".
 */
#include <windows.h>
#include <stdio.h>

static int try_create(const char *what, HWND parent, DWORD style)
{
    HWND h;
    SetLastError(0);
    h = CreateWindowExA(0, "winmsg_cls", "winmsg", style, 0, 0, 16, 16,
                        parent, NULL, GetModuleHandleA(NULL), NULL);
    printf("[%s] %-26s hwnd=%p err=%lu\n", h ? " ok " : "FAIL", what,
           (void *)h, (unsigned long)GetLastError());
    fflush(stdout);
    if (!h) return 1;
    DestroyWindow(h);
    return 0;
}

int main(void)
{
    WNDCLASSA wc;
    int failed = 0;

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "winmsg_cls";
    if (!RegisterClassA(&wc)) {
        printf("FAIL RegisterClass err=%lu\n", (unsigned long)GetLastError());
        return 2;
    }

    failed += try_create("top-level", NULL, WS_OVERLAPPEDWINDOW);
    failed += try_create("message-only", HWND_MESSAGE, 0);

    printf("RESULT: %s\n", failed ? "windows broken" : "windows ok");
    return failed ? 1 : 0;
}
