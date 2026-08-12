/* is there a WMA decoder behind Media Foundation?
 *
 * a runtime with no WMA decoder MFT plays PCM sound effects fine and is silent
 * for every .xwm music track and voice line, with nothing logged anywhere.
 * audio companion to mfprobe.c, but a diagnosis rather than a gate, so it
 * reports every format instead of failing on the first.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o wmaprobe.exe wmaprobe.c -lmfplat -lmfuuid -lole32
 */
#define COBJMACROS
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <stdio.h>

static const GUID guid_major_audio =
    {0x73647561, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};
static const GUID guid_category_audio_decoder =
    {0x9ea73fb4, 0xef7a, 0x4559, {0x8d, 0x5d, 0x71, 0x9d, 0x8f, 0x04, 0x26, 0xc7}};

/* audio subtypes are the wave format tag in data1 of a fixed GUID template */
static GUID audio_subtype(unsigned tag)
{
    GUID g = {0, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};
    g.Data1 = tag;
    return g;
}

static const struct { unsigned tag; const char *name; } FORMATS[] = {
    { 0x0160, "WMAUDIO1"   },
    { 0x0161, "WMAUDIO2  <- skyrim music/voice" },
    { 0x0162, "WMAUDIO3 (pro)" },
    { 0x0163, "WMAUDIO_LOSSLESS" },
    { 0x0166, "XMAUDIO2"   },
    { 0x1610, "AAC (raw)"  },
    { 0x0055, "MP3"        },
};

int main(void)
{
    /* MFTEnumEx hands back activates that instantiate through COM, so a
     * missing CoInitializeEx here would fail the activation for a reason that
     * has nothing to do with the runtime */
    HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    printf("[%s] CoInitializeEx 0x%08lx\n", SUCCEEDED(hr) ? " ok " : "fail", (unsigned long)hr);

    hr = MFStartup(MF_VERSION, MFSTARTUP_FULL);
    printf("[%s] MFStartup 0x%08lx\n", SUCCEEDED(hr) ? " ok " : "fail", (unsigned long)hr);
    if (FAILED(hr)) return 1;

    int missing = 0;
    for (unsigned i = 0; i < ARRAYSIZE(FORMATS); i++) {
        MFT_REGISTER_TYPE_INFO in = { guid_major_audio, audio_subtype(FORMATS[i].tag) };
        IMFActivate **acts = NULL;
        UINT32 count = 0;

        hr = MFTEnumEx(guid_category_audio_decoder, MFT_ENUM_FLAG_ALL,
                       &in, NULL, &acts, &count);
        if (FAILED(hr) || !count) {
            printf("[fail] 0x%04x %-30s no decoder (hr 0x%08lx)\n",
                   FORMATS[i].tag, FORMATS[i].name, (unsigned long)hr);
            missing++;
        } else {
            printf("[ ok ] 0x%04x %-30s %u decoder%s\n",
                   FORMATS[i].tag, FORMATS[i].name, count, count == 1 ? "" : "s");
        }
        /* registration is only a registry entry and survives with no working
         * GStreamer underneath, so activate the one that matters */
        if (count && FORMATS[i].tag == 0x0161) {
            IMFTransform *mft = NULL;
            hr = IMFActivate_ActivateObject(acts[0], &IID_IMFTransform, (void **)&mft);
            printf("[%s]        activate WMAUDIO2 decoder            0x%08lx\n",
                   SUCCEEDED(hr) ? " ok " : "fail", (unsigned long)hr);
            if (SUCCEEDED(hr)) {
                IMFMediaType *mt = NULL;
                if (SUCCEEDED(MFCreateMediaType(&mt))) {
                    IMFMediaType_SetGUID(mt, &MF_MT_MAJOR_TYPE, &guid_major_audio);
                    GUID sub = audio_subtype(0x0161);
                    IMFMediaType_SetGUID(mt, &MF_MT_SUBTYPE, &sub);
                    IMFMediaType_SetUINT32(mt, &MF_MT_AUDIO_NUM_CHANNELS, 2);
                    IMFMediaType_SetUINT32(mt, &MF_MT_AUDIO_SAMPLES_PER_SECOND, 44100);
                    IMFMediaType_SetUINT32(mt, &MF_MT_AUDIO_BLOCK_ALIGNMENT, 2972);
                    IMFMediaType_SetUINT32(mt, &MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 16000);
                    IMFMediaType_SetUINT32(mt, &MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
                    hr = IMFTransform_SetInputType(mft, 0, mt, 0);
                    printf("[%s]        SetInputType WMAUDIO2 44k/2ch        0x%08lx\n",
                           SUCCEEDED(hr) ? " ok " : "fail", (unsigned long)hr);
                    if (FAILED(hr)) missing++;
                    IMFMediaType_Release(mt);
                }
                IMFTransform_Release(mft);
            } else {
                missing++;
            }
        }
        for (UINT32 k = 0; k < count; k++) IMFActivate_Release(acts[k]);
        if (acts) CoTaskMemFree(acts);
    }

    printf("--- %d of %d audio formats have no decoder\n", missing, (int)ARRAYSIZE(FORMATS));
    MFShutdown();
    return missing ? 1 : 0;
}
