/* is there a real media pipeline behind Media Foundation?
 *
 * winegstreamer registers its decoders as MFTs at init, and it only gets that
 * far if gst_init found a plugin registry. a runtime whose GStreamer plugin
 * path is wrong still starts, still answers MFStartup, and simply has no
 * decoders -- which shows up much later as a game hanging on its intro video.
 * so ask for an H.264 decoder specifically.
 */
#define COBJMACROS
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <stdio.h>

int main(void)
{
    MFT_REGISTER_TYPE_INFO in = { MFMediaType_Video, MFVideoFormat_H264 };
    IMFActivate **acts = NULL;
    UINT32 count = 0;
    HRESULT hr;
    UINT32 i;

    hr = MFStartup(MF_VERSION, MFSTARTUP_FULL);
    if (FAILED(hr))
    {
        printf("FAIL: MFStartup %08lx\n", (unsigned long)hr);
        return 1;
    }
    printf("[ ok ] MFStartup\n");

    hr = MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER, MFT_ENUM_FLAG_ALL, &in, NULL,
                   &acts, &count);
    if (FAILED(hr))
    {
        printf("FAIL: MFTEnumEx %08lx\n", (unsigned long)hr);
        return 1;
    }
    printf("[info] h264 video decoders: %u\n", count);

    for (i = 0; i < count; i++)
    {
        WCHAR name[256];
        UINT32 len = 0;
        if (SUCCEEDED(IMFActivate_GetString(acts[i], &MFT_FRIENDLY_NAME_Attribute,
                                            name, ARRAY_SIZE(name), &len)))
            printf("[info]   %ls\n", name);
        IMFActivate_Release(acts[i]);
    }
    CoTaskMemFree(acts);

    MFShutdown();

    if (!count)
    {
        printf("FAIL: no h264 decoder registered\n");
        return 1;
    }
    printf("RESULT: media foundation has decoders\n");
    return 0;
}
