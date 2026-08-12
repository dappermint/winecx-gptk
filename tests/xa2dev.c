/* what does XAudio2 2.7 tell a game the speakers are?
 *
 * a game builds its mastering voice from GetDeviceDetails, and X3DAudio then
 * pans for whatever layout that reported. report 5.1 on stereo hardware and
 * every sound in front of the listener lands in a front-centre channel that
 * does not exist, while sounds behind land on the rear pair and play normally.
 *
 * the 2.7 interface is declared by hand: mingw's xaudio2.h is 2.8+, which
 * dropped GetDeviceDetails entirely.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o xa2dev.exe xa2dev.c -lole32
 */
#include <windows.h>
#include <mmreg.h>
#include <stdio.h>

static const CLSID CLSID_XAudio2_7 =
    {0x5a508685, 0xa254, 0x4fba, {0x9b, 0x82, 0x9a, 0x24, 0xb0, 0x03, 0x06, 0xaf}};
static const IID IID_IXAudio2_7 =
    {0x8bcf1f58, 0x9fe7, 0x4583, {0x8a, 0xc6, 0xe2, 0xad, 0xc4, 0x65, 0xc8, 0xbb}};

typedef struct {
    WCHAR DeviceID[256];
    WCHAR DisplayName[256];
    DWORD Role;
    WAVEFORMATEXTENSIBLE OutputFormat;
} XA2_DEVICE_DETAILS;

typedef struct IXAudio2_7 IXAudio2_7;
typedef struct {
    HRESULT (WINAPI *QueryInterface)(IXAudio2_7 *, const IID *, void **);
    ULONG   (WINAPI *AddRef)(IXAudio2_7 *);
    ULONG   (WINAPI *Release)(IXAudio2_7 *);
    HRESULT (WINAPI *GetDeviceCount)(IXAudio2_7 *, UINT32 *);
    HRESULT (WINAPI *GetDeviceDetails)(IXAudio2_7 *, UINT32, XA2_DEVICE_DETAILS *);
} IXAudio2_7Vtbl;
struct IXAudio2_7 { const IXAudio2_7Vtbl *lpVtbl; };

static const struct { DWORD bit; const char *name; } SPEAKERS[] = {
    { 0x1, "FL" }, { 0x2, "FR" }, { 0x4, "FC" }, { 0x8, "LFE" },
    { 0x10, "BL" }, { 0x20, "BR" }, { 0x40, "FLC" }, { 0x80, "FRC" },
    { 0x100, "BC" }, { 0x200, "SL" }, { 0x400, "SR" },
};

int main(void)
{
    IXAudio2_7 *xa = NULL;
    UINT32 count = 0;

    CoInitializeEx(NULL, COINIT_MULTITHREADED);
    HRESULT hr = CoCreateInstance(&CLSID_XAudio2_7, NULL, CLSCTX_INPROC_SERVER,
                                  &IID_IXAudio2_7, (void **)&xa);
    if (FAILED(hr)) {
        printf("[fail] CoCreateInstance XAudio2 2.7 0x%08lx\n", (unsigned long)hr);
        return 1;
    }
    if (FAILED(xa->lpVtbl->GetDeviceCount(xa, &count))) {
        printf("[fail] GetDeviceCount\n");
        return 1;
    }
    printf("[info] %u device%s\n", count, count == 1 ? "" : "s");

    for (UINT32 i = 0; i < count; i++) {
        XA2_DEVICE_DETAILS d;
        memset(&d, 0, sizeof(d));
        if (FAILED(xa->lpVtbl->GetDeviceDetails(xa, i, &d))) continue;

        WAVEFORMATEX *f = &d.OutputFormat.Format;
        DWORD mask = d.OutputFormat.dwChannelMask;
        printf("[dev ] %u: %ls\n", i, d.DisplayName);
        printf("       channels=%u rate=%lu role=%lu mask=0x%08lx:",
               f->nChannels, f->nSamplesPerSec, d.Role, mask);
        for (unsigned k = 0; k < ARRAYSIZE(SPEAKERS); k++)
            if (mask & SPEAKERS[k].bit) printf(" %s", SPEAKERS[k].name);
        printf("\n");
    }

    xa->lpVtbl->Release(xa);
    return 0;
}
