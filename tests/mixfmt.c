/* what channel layout does the runtime hand a game?
 *
 * xaudio2 pans dialogue to the centre channel on any layout that has one, so a
 * runtime reporting 5.1 into stereo hardware loses every line spoken by an npc
 * the player is facing while off-axis lines still play through L/R.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o mixfmt.exe mixfmt.c -lole32
 */
#define COBJMACROS
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <stdio.h>

static const struct { DWORD bit; const char *name; } SPEAKERS[] = {
    { 0x1, "FL" }, { 0x2, "FR" }, { 0x4, "FC" }, { 0x8, "LFE" },
    { 0x10, "BL" }, { 0x20, "BR" }, { 0x40, "FLC" }, { 0x80, "FRC" },
    { 0x100, "BC" }, { 0x200, "SL" }, { 0x400, "SR" },
};

int main(void)
{
    IMMDeviceEnumerator *en = NULL;
    IMMDevice *dev = NULL;
    IAudioClient *ac = NULL;
    WAVEFORMATEX *fmt = NULL;

    CoInitializeEx(NULL, COINIT_MULTITHREADED);

    if (FAILED(CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL,
                                &IID_IMMDeviceEnumerator, (void **)&en))) {
        printf("[fail] no device enumerator\n");
        return 1;
    }
    if (FAILED(IMMDeviceEnumerator_GetDefaultAudioEndpoint(en, eRender, eConsole, &dev))) {
        printf("[fail] no default render endpoint\n");
        return 1;
    }
    if (FAILED(IMMDevice_Activate(dev, &IID_IAudioClient, CLSCTX_ALL, NULL, (void **)&ac))) {
        printf("[fail] cannot activate audio client\n");
        return 1;
    }
    if (FAILED(IAudioClient_GetMixFormat(ac, &fmt))) {
        printf("[fail] no mix format\n");
        return 1;
    }

    printf("[fmt ] channels=%u rate=%lu bits=%u\n",
           fmt->nChannels, fmt->nSamplesPerSec, fmt->wBitsPerSample);

    if (fmt->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        DWORD mask = ((WAVEFORMATEXTENSIBLE *)fmt)->dwChannelMask;
        printf("[mask] 0x%08lx:", mask);
        for (unsigned i = 0; i < ARRAYSIZE(SPEAKERS); i++)
            if (mask & SPEAKERS[i].bit) printf(" %s", SPEAKERS[i].name);
        printf("\n");
        printf("RESULT: %s\n", (mask & 0x4) ? "has a centre channel, dialogue pans there"
                                            : "no centre channel");
    } else {
        printf("RESULT: plain %u-channel format, no channel mask\n", fmt->nChannels);
    }

    CoTaskMemFree(fmt);
    return 0;
}
