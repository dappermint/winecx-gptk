/* is there a real media pipeline behind Media Foundation?
 *
 * winegstreamer registers its decoders as MFTs at init, and it only gets that
 * far if gst_init found a plugin registry. a runtime whose GStreamer plugin
 * path is wrong still starts, still answers MFStartup, and simply has no
 * decoders -- which shows up much later as a game hanging on its intro video.
 * so ask for an H.264 decoder specifically.
 *
 * the GUIDs are defined here rather than linked from mfuuid: this runs as a
 * build gate, and a missing symbol in whatever mfuuid the runner's mingw ships
 * would fail the build after the whole compile rather than report anything.
 */
#define COBJMACROS
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <stdio.h>

static const GUID guid_major_video =
    {0x73646976, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};
static const GUID guid_subtype_h264 =
    {0x34363248, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};
static const GUID guid_category_video_decoder =
    {0xd6c02d4b, 0x6833, 0x45b4, {0x97, 0x1a, 0x05, 0xa4, 0xb0, 0x4b, 0xab, 0x91}};
static const GUID guid_friendly_name =
    {0x314ffbae, 0x5b41, 0x4c95, {0x9c, 0x19, 0x4e, 0x7d, 0x58, 0x6f, 0xac, 0xe3}};

int main(void)
{
    MFT_REGISTER_TYPE_INFO in;
    IMFActivate **acts = NULL;
    UINT32 count = 0;
    HRESULT hr;
    UINT32 i;

    in.guidMajorType = guid_major_video;
    in.guidSubtype = guid_subtype_h264;

    hr = MFStartup(MF_VERSION, MFSTARTUP_FULL);
    if (FAILED(hr))
    {
        printf("FAIL: MFStartup %08lx\n", (unsigned long)hr);
        return 1;
    }
    printf("[ ok ] MFStartup\n");

    hr = MFTEnumEx(guid_category_video_decoder, MFT_ENUM_FLAG_ALL, &in, NULL,
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
        if (SUCCEEDED(IMFActivate_GetString(acts[i], &guid_friendly_name,
                                            name, 256, &len)))
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
