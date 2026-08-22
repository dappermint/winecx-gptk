/* d3d12shim: NV12 support for the d3d12.dll builtin slot.
 *
 * D3DMetal answers CheckFeatureSupport(NV12) with nothing, so xrEngine EE
 * takes a video upload path that no windows machine executes and that writes
 * packed rows into a double-pitch destination. This claims NV12, and backs
 * each NV12 texture with two real textures, R8 for luma and R8G8 at half size
 * for chroma, translating the calls that touch them.
 *
 * The app is handed the luma texture's own pointer rather than a wrapper
 * object, so any call not translated here still lands on a real resource:
 * stale chroma rather than a jump into a bogus vtable.
 */
#define COBJMACROS
#include <windows.h>
#include <d3d12.h>
#include <d3d12video.h>
#include <dxva.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>

/* mingw's d3d12video.h stops short of the enumeration feature structs */
typedef struct
{
    UINT NodeIndex;
    UINT ProfileCount;
} shim_DECODE_PROFILE_COUNT;

typedef struct
{
    UINT NodeIndex;
    UINT ProfileCount;
    GUID *pProfiles;
} shim_DECODE_PROFILES;

typedef struct
{
    UINT NodeIndex;
    D3D12_VIDEO_DECODE_CONFIGURATION Configuration;
    UINT FormatCount;
} shim_DECODE_FORMAT_COUNT;

typedef struct
{
    UINT NodeIndex;
    D3D12_VIDEO_DECODE_CONFIGURATION Configuration;
    UINT FormatCount;
    DXGI_FORMAT *pOutputFormats;
} shim_DECODE_FORMATS;

typedef struct
{
    D3D12_VIDEO_DECODER_HEAP_DESC VideoDecoderHeapDesc;
    UINT64 MemoryPoolL0Size;
    UINT64 MemoryPoolL1Size;
} shim_DECODER_HEAP_SIZE;

typedef struct
{
    WINBOOL IOCoherent;
} shim_VIDEO_ARCHITECTURE;

typedef struct
{
    UINT NodeIndex;
    UINT MaxInputStreams;
} shim_PROCESS_MAX_INPUT_STREAMS;

typedef struct
{
    UINT NodeIndex;
    D3D12_VIDEO_PROCESS_DEINTERLACE_FLAGS DeinterlaceMode;
    D3D12_VIDEO_PROCESS_FILTER_FLAGS Filters;
    WINBOOL EnableAutoProcessing;
    UINT PastFrames;
    UINT FutureFrames;
} shim_PROCESS_REFERENCE_INFO;

/* D3D12_VIDEO_DECODE_PROFILE_H264, same guid as DXVA2_ModeH264_VLD_NoFGT */
static const GUID profile_h264 =
        { 0x1b81be68, 0xa0c7, 0x11d3, { 0xb9, 0x84, 0x00, 0xc0, 0x4f, 0x2e, 0x73, 0xc5 } };

#define SLOT(vtbl, method) (offsetof(vtbl, method) / sizeof(void *))

static HMODULE real_mod;
static FILE *logf;
static CRITICAL_SECTION cs;
static BOOL cs_ready;

#define LOG(...) do { if (logf) { fprintf(logf, __VA_ARGS__); fflush(logf); } } while (0)

/* The recon lines fire per texture and per copy, which is thousands of flushed
 * writes a second in a real game. They stay off unless D3D12SHIM_LOG=1; what is
 * left is setup, the video negotiation and anything that failed. */
static BOOL want_log(void)
{
    static int on = -1;

    if (on < 0)
    {
        char buf[8];

        on = GetEnvironmentVariableA("D3D12SHIM_LOG", buf, sizeof(buf)) > 0 && buf[0] == '1';
    }
    return on > 0;
}

#define DBG(...) do { if (want_log()) LOG(__VA_ARGS__); } while (0)

#include "yuvblit.h"

static void *real_fn(const char *name)
{
    if (!real_mod)
    {
        logf = fopen("C:\\d3d12shim.log", "a");
        InitializeCriticalSection(&cs);
        cs_ready = TRUE;
        real_mod = LoadLibraryA("d3dmt.dll");
        LOG("=== shim up: d3dmt.dll=%p err=%lu\n", (void *)real_mod, GetLastError());
    }
    return real_mod ? (void *)GetProcAddress(real_mod, name) : NULL;
}

/* ---- the NV12 pairs ---- */

struct nv12
{
    ID3D12Resource *luma;    /* what the app holds */
    ID3D12Resource *chroma;
    UINT width, height;
};

#define MAX_NV12 64
static struct nv12 pairs[MAX_NV12];
static unsigned int npairs;

/* Slots are never compacted and the array is static, so the returned pointer
 * stays valid memory; a dropped slot just stops matching. The one window left is
 * an app destroying a texture while another thread records a barrier on it, which
 * is undefined in D3D12 regardless of what we do here. */
static struct nv12 *find_pair(void *res)
{
    struct nv12 *found = NULL;
    unsigned int i;

    if (!res || !cs_ready)
        return NULL;
    EnterCriticalSection(&cs);
    for (i = 0; i < npairs; i++)
        if (pairs[i].luma == res)
        {
            found = &pairs[i];
            break;
        }
    LeaveCriticalSection(&cs);
    return found;
}

/* Dropping a pair when the app's texture dies.
 *
 * D3D12 releases a private-data interface when the object it is attached to is
 * destroyed, which is the only destruction notification the API offers. It
 * matters because fix_barriers runs on the app's own command list for the whole
 * life of the process: a slot still holding a freed luma pointer keeps matching
 * once the allocator hands that address to something else, and every barrier on
 * the new resource is then mirrored onto a chroma texture nobody owns. Helldivers
 * 2 hit that the moment its intro video ended. */
static const GUID IID_shim_pair_watch =
    { 0x6f2a9c31, 0x4b8d, 0x4e57, { 0x9a, 0x0e, 0x7d, 0x35, 0xc1, 0x88, 0x2f, 0x64 } };

struct pair_watch
{
    void **vtbl;
    LONG ref;
    unsigned int slot;
};

static void *pair_watch_vtbl[3];

static void pair_drop(unsigned int slot)
{
    ID3D12Resource *chroma = NULL;

    if (!cs_ready)
        return;
    EnterCriticalSection(&cs);
    if (slot < npairs && pairs[slot].luma)
    {
        /* clear the match first, so find_pair cannot hand this slot out again */
        pairs[slot].luma = NULL;
        chroma = pairs[slot].chroma;
        pairs[slot].chroma = NULL;
    }
    LeaveCriticalSection(&cs);

    if (chroma)
    {
        LOG("  pair %u dropped with its luma texture\n", slot);
        ID3D12Resource_Release(chroma);
    }
}

static ULONG WINAPI pair_watch_AddRef(void *this_)
{
    return InterlockedIncrement(&((struct pair_watch *)this_)->ref);
}

static ULONG WINAPI pair_watch_Release(void *this_)
{
    struct pair_watch *w = this_;
    LONG ref = InterlockedDecrement(&w->ref);

    if (!ref)
    {
        pair_drop(w->slot);
        HeapFree(GetProcessHeap(), 0, w);
    }
    return ref;
}

static HRESULT WINAPI pair_watch_QueryInterface(void *this_, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    if (IsEqualGUID(riid, &IID_IUnknown) || IsEqualGUID(riid, &IID_shim_pair_watch))
    {
        pair_watch_AddRef(this_);
        *out = this_;
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

/* Attaches a watcher so the slot is dropped when the texture is destroyed. If it
 * cannot be attached the slot keeps the old never-dropped behaviour rather than
 * being torn down while the app still holds the texture. */
static void pair_watch_attach(ID3D12Resource *luma, unsigned int slot)
{
    struct pair_watch *w;
    HRESULT hr;

    if (!(w = HeapAlloc(GetProcessHeap(), 0, sizeof(*w))))
        return;
    w->vtbl = pair_watch_vtbl;
    w->ref = 1;
    w->slot = slot;

    hr = ID3D12Resource_SetPrivateDataInterface(luma, &IID_shim_pair_watch, (IUnknown *)w);
    if (SUCCEEDED(hr))
        pair_watch_Release(w);   /* the texture holds the only reference now */
    else
    {
        LOG("  pair %u has no destruction notification, 0x%08lx\n", slot, hr);
        HeapFree(GetProcessHeap(), 0, w);
    }
}

/* ---- watching a video texture's own methods ---- */

#define RES_SLOTS 24
static void *res_vtbl[RES_SLOTS];
static void **res_orig;

struct watched { void *res; UINT w, h, fmt; };
static struct watched watched[32];
static unsigned int nwatched;
static BOOL dumped;
static unsigned int videoframes;

static struct watched *find_watched(void *res)
{
    unsigned int i;

    for (i = 0; i < nwatched; i++)
        if (watched[i].res == res)
            return &watched[i];
    return NULL;
}

typedef HRESULT (WINAPI *pfn_map)(void *, UINT, const D3D12_RANGE *, void **);
typedef HRESULT (WINAPI *pfn_wts)(void *, UINT, const D3D12_BOX *, const void *, UINT, UINT);

static HRESULT WINAPI shim_Map(void *res, UINT sub, const D3D12_RANGE *range, void **data)
{
    HRESULT hr = ((pfn_map)res_orig[8])(res, sub, range, data);
    struct watched *w = find_watched(res);

    DBG("Map(video %p sub=%u) -> 0x%08lx ptr=%p  [%ux%u fmt=%u]\n", res, sub, hr,
        data ? *data : NULL, w ? w->w : 0, w ? w->h : 0, w ? w->fmt : 0);
    return hr;
}

static HRESULT WINAPI shim_WriteToSubresource(void *res, UINT sub, const D3D12_BOX *box,
        const void *src, UINT row_pitch, UINT depth_pitch)
{
    struct watched *w = find_watched(res);

    DBG("WriteToSubresource(video %p sub=%u) src_row_pitch=%u depth=%u box=%s  [%ux%u fmt=%u]\n",
        res, sub, row_pitch, depth_pitch, box ? "yes" : "null",
        w ? w->w : 0, w ? w->h : 0, w ? w->fmt : 0);
    if (box)
        DBG("  box left=%u top=%u right=%u bottom=%u\n", box->left, box->top, box->right, box->bottom);
    return ((pfn_wts)res_orig[12])(res, sub, box, src, row_pitch, depth_pitch);
}

static void watch_resource(void *res, UINT w, UINT h, UINT fmt)
{
    void ***obj = res;

    if (nwatched >= 32)
        return;
    if (!res_orig)
    {
        unsigned int i;

        res_orig = *obj;
        for (i = 0; i < RES_SLOTS; i++)
            res_vtbl[i] = res_orig[i];
        res_vtbl[8] = (void *)shim_Map;
        res_vtbl[12] = (void *)shim_WriteToSubresource;
        LOG("resource vtable copied for watching\n");
    }
    if (*obj != res_orig && *obj != (void **)res_vtbl)
    {
        LOG("video texture %p has a different vtable, not watching\n", res);
        return;
    }
    *obj = (void **)res_vtbl;
    watched[nwatched].res = res;
    watched[nwatched].w = w;
    watched[nwatched].h = h;
    watched[nwatched].fmt = fmt;
    nwatched++;
    LOG("watching video texture %p (%ux%u fmt=%u)\n", res, w, h, fmt);
}

/* ---- device ---- */

#define DEV_SLOTS 64
static void *dev_vtbl[DEV_SLOTS];
static void **dev_orig;

typedef HRESULT (WINAPI *pfn_cfs)(void *, D3D12_FEATURE, void *, UINT);
typedef HRESULT (WINAPI *pfn_ccr)(void *, const D3D12_HEAP_PROPERTIES *, D3D12_HEAP_FLAGS,
        const D3D12_RESOURCE_DESC *, D3D12_RESOURCE_STATES, const D3D12_CLEAR_VALUE *, REFIID, void **);
typedef void (WINAPI *pfn_csrv)(void *, ID3D12Resource *, const D3D12_SHADER_RESOURCE_VIEW_DESC *,
        D3D12_CPU_DESCRIPTOR_HANDLE);
typedef void (WINAPI *pfn_gcf)(void *, const D3D12_RESOURCE_DESC *, UINT, UINT, UINT64,
        D3D12_PLACED_SUBRESOURCE_FOOTPRINT *, UINT *, UINT64 *, UINT64 *);
typedef HRESULT (WINAPI *pfn_ccl)(void *, UINT, D3D12_COMMAND_LIST_TYPE, void *, void *, REFIID, void **);
typedef HRESULT (WINAPI *pfn_cpr)(void *, void *, UINT64, const D3D12_RESOURCE_DESC *,
        D3D12_RESOURCE_STATES, const D3D12_CLEAR_VALUE *, REFIID, void **);

static void wrap_command_list(void *list);

/* ---- ID3D12VideoDevice ----
 *
 * D3DMetal exposes no video decode at all, so xrEngine EE's QI for
 * IID_ID3D12VideoDevice fails and it takes a software path that produces half
 * resolution, chroma-free frames. The interface is grown here; the decode
 * itself goes to the h264 MFT that winegstreamer already provides inside this
 * process, which reaches VideoToolbox through libgstapplemedia.
 *
 * The video queue, allocator and command list are shells over real DIRECT
 * objects, so fences and submission ordering stay Apple's problem.
 */

static ID3D12Device *real_device;
static unsigned int decode_calls;

/* The dumps below write tens of megabytes per playback, so they stay off unless
 * asked for: set D3D12SHIM_DUMP=1 to collect them. */
static BOOL want_dumps(void)
{
    static int on = -1;

    if (on < 0)
    {
        char buf[8];

        on = GetEnvironmentVariableA("D3D12SHIM_DUMP", buf, sizeof(buf)) > 0 && buf[0] == '1';
        LOG("frame and argument dumps are %s\n", on ? "ON" : "off");
    }
    return on > 0;
}

/* xrEngine declares full range for content that is really studio range, so
 * obeying the label the way a real driver does leaves black at 16 and the video
 * washed out. Looking right beats matching windows, so the declaration is
 * treated as studio by default; D3D12SHIM_VIDEO_RANGE=label obeys the app
 * exactly and gets windows parity back. */
static DXGI_COLOR_SPACE_TYPE override_cs(DXGI_COLOR_SPACE_TYPE cs)
{
    static int mode = -1;

    if (mode < 0)
    {
        char buf[16];

        mode = 1;
        if (GetEnvironmentVariableA("D3D12SHIM_VIDEO_RANGE", buf, sizeof(buf)) > 0)
        {
            if (!_strnicmp(buf, "label", 5) || !_strnicmp(buf, "full", 4)) mode = 0;
            else if (!_strnicmp(buf, "studio", 6)) mode = 1;
        }
        LOG("video range: %s\n", mode ? "declared full range treated as studio"
                : "as declared by the app");
    }
    if (mode == 1 && cs == DXGI_COLOR_SPACE_YCBCR_FULL_G22_LEFT_P709)
        return DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P709;
    return cs;
}

static const char *guidstr(const GUID *g)
{
    static char bufs[4][48];
    static unsigned int next;
    char *b = bufs[(next++) & 3];

    if (!g)
        return "(null)";
    sprintf(b, "%08lx-%04x-%04x-%02x%02x%02x%02x%02x%02x%02x%02x", g->Data1, g->Data2, g->Data3,
            g->Data4[0], g->Data4[1], g->Data4[2], g->Data4[3],
            g->Data4[4], g->Data4[5], g->Data4[6], g->Data4[7]);
    return b;
}

/* The video-only resource states mean nothing to the plain textures underneath
 * ours on a direct queue. Each one is mapped to the state this implementation
 * actually needs, so an app that follows the api contract leaves every resource
 * exactly as the conversion pass wants it and no extra barriers are needed:
 * process reads are sampled, process writes are drawn into. */
static D3D12_RESOURCE_STATES fix_state(D3D12_RESOURCE_STATES state)
{
    if (state & (D3D12_RESOURCE_STATE_VIDEO_PROCESS_WRITE | D3D12_RESOURCE_STATE_VIDEO_ENCODE_WRITE))
        return D3D12_RESOURCE_STATE_RENDER_TARGET;
    if (state & D3D12_RESOURCE_STATE_VIDEO_DECODE_WRITE)
        return D3D12_RESOURCE_STATE_COPY_DEST;
    if (state & (D3D12_RESOURCE_STATE_VIDEO_PROCESS_READ | D3D12_RESOURCE_STATE_VIDEO_ENCODE_READ))
        return D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    if (state & D3D12_RESOURCE_STATE_VIDEO_DECODE_READ)
        return D3D12_RESOURCE_STATE_COPY_SOURCE;
    return state;
}

/* GetDesc returns a struct by value, which the C bindings will not call */
static D3D12_RESOURCE_DESC res_desc(ID3D12Resource *res)
{
    typedef D3D12_RESOURCE_DESC * (WINAPI *pfn)(ID3D12Resource *, D3D12_RESOURCE_DESC *);
    D3D12_RESOURCE_DESC d;
    void **vt = *(void ***)res;

    memset(&d, 0, sizeof(d));
    ((pfn)vt[SLOT(ID3D12ResourceVtbl, GetDesc)])(res, &d);
    return d;
}

/* fix those states and give every NV12 pair's chroma texture the same barrier */
static UINT fix_barriers(const D3D12_RESOURCE_BARRIER *bars, UINT count,
        D3D12_RESOURCE_BARRIER *out, UINT max)
{
    UINT i, n = 0;

    for (i = 0; i < count && n < max; i++)
    {
        out[n] = bars[i];
        if (bars[i].Type == D3D12_RESOURCE_BARRIER_TYPE_TRANSITION)
        {
            out[n].Transition.StateBefore = fix_state(bars[i].Transition.StateBefore);
            out[n].Transition.StateAfter = fix_state(bars[i].Transition.StateAfter);
        }
        n++;
    }
    for (i = 0; i < count && n < max; i++)
    {
        struct nv12 *pair;

        if (bars[i].Type != D3D12_RESOURCE_BARRIER_TYPE_TRANSITION)
            continue;
        if (!(pair = find_pair(bars[i].Transition.pResource)))
            continue;
        out[n] = bars[i];
        out[n].Transition.pResource = pair->chroma;
        out[n].Transition.StateBefore = fix_state(bars[i].Transition.StateBefore);
        out[n].Transition.StateAfter = fix_state(bars[i].Transition.StateAfter);
        n++;
    }
    return n;
}

/* every object below starts with a vtable pointer and a refcount */
struct vobj
{
    void **vtbl;
    LONG ref;
};

struct vdecoder
{
    void **vtbl;
    LONG ref;
    D3D12_VIDEO_DECODER_DESC desc;
};

struct vheap
{
    void **vtbl;
    LONG ref;
    D3D12_VIDEO_DECODER_HEAP_DESC desc;
};

struct vlist
{
    void **vtbl;
    LONG ref;
    ID3D12GraphicsCommandList *inner;
};

struct vprocessor
{
    void **vtbl;
    LONG ref;
    UINT node_mask;
    D3D12_VIDEO_PROCESS_OUTPUT_STREAM_DESC out;
    UINT num_in;
    D3D12_VIDEO_PROCESS_INPUT_STREAM_DESC in[2];
};

static void *vdev_vtbl[16];
static void *vdecoder_vtbl[9];
static void *vheap_vtbl[9];
static void *vlist_vtbl[23];
static void *vproc_vtbl[23];
static void *vprocessor_vtbl[12];

static struct vobj video_device;

static ULONG WINAPI vobj_AddRef(void *this_)
{
    struct vobj *o = this_;

    return InterlockedIncrement(&o->ref);
}

static ULONG WINAPI vobj_Release(void *this_)
{
    struct vobj *o = this_;
    LONG ref = InterlockedDecrement(&o->ref);

    if (!ref && o != &video_device)
    {
        if ((o->vtbl == vlist_vtbl || o->vtbl == vproc_vtbl) && ((struct vlist *)o)->inner)
            ID3D12GraphicsCommandList_Release(((struct vlist *)o)->inner);
        HeapFree(GetProcessHeap(), 0, o);
    }
    return ref;
}

static HRESULT WINAPI vobj_GetPrivateData(void *this_, REFGUID guid, UINT *size, void *data)
{
    if (size) *size = 0;
    return E_INVALIDARG;
}

static HRESULT WINAPI vobj_SetPrivateData(void *this_, REFGUID guid, UINT size, const void *data)
{
    return S_OK;
}

static HRESULT WINAPI vobj_SetPrivateDataInterface(void *this_, REFGUID guid, const IUnknown *unk)
{
    return S_OK;
}

static HRESULT WINAPI vobj_SetName(void *this_, const WCHAR *name)
{
    return S_OK;
}

static HRESULT WINAPI vobj_GetDevice(void *this_, REFIID riid, void **out)
{
    if (!real_device)
        return E_FAIL;
    return ID3D12Device_QueryInterface(real_device, riid, out);
}

static HRESULT WINAPI v_notimpl(void *this_, void *a, void *b, void *c, void *d, void *e, void *f)
{
    LOG("video: unimplemented method on %p\n", this_);
    return E_NOTIMPL;
}

/* ---- the decoder and its heap: nothing to own, the MFT holds the real state ---- */

static HRESULT WINAPI vdecoder_QueryInterface(void *this_, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    if (IsEqualGUID(riid, &IID_IUnknown) || IsEqualGUID(riid, &IID_ID3D12VideoDecoder))
    {
        *out = this_;
        vobj_AddRef(this_);
        return S_OK;
    }
    LOG("decoder QI %s refused\n", guidstr(riid));
    *out = NULL;
    return E_NOINTERFACE;
}

static D3D12_VIDEO_DECODER_DESC * WINAPI vdecoder_GetDesc(void *this_, D3D12_VIDEO_DECODER_DESC *ret)
{
    *ret = ((struct vdecoder *)this_)->desc;
    return ret;
}

static HRESULT WINAPI vheap_QueryInterface(void *this_, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    if (IsEqualGUID(riid, &IID_IUnknown) || IsEqualGUID(riid, &IID_ID3D12VideoDecoderHeap))
    {
        *out = this_;
        vobj_AddRef(this_);
        return S_OK;
    }
    LOG("decoder heap QI %s refused\n", guidstr(riid));
    *out = NULL;
    return E_NOINTERFACE;
}

static D3D12_VIDEO_DECODER_HEAP_DESC * WINAPI vheap_GetDesc(void *this_,
        D3D12_VIDEO_DECODER_HEAP_DESC *ret)
{
    *ret = ((struct vheap *)this_)->desc;
    return ret;
}

/* ---- the video decode command list ---- */

static HRESULT WINAPI vlist_QueryInterface(void *this_, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    if (IsEqualGUID(riid, &IID_IUnknown) || IsEqualGUID(riid, &IID_ID3D12Object)
            || IsEqualGUID(riid, &IID_ID3D12DeviceChild) || IsEqualGUID(riid, &IID_ID3D12CommandList)
            || IsEqualGUID(riid, &IID_ID3D12VideoDecodeCommandList)
            || IsEqualGUID(riid, &IID_ID3D12VideoDecodeCommandList1)
            || IsEqualGUID(riid, &IID_ID3D12VideoProcessCommandList))
    {
        *out = this_;
        vobj_AddRef(this_);
        return S_OK;
    }
    LOG("video list QI %s refused\n", guidstr(riid));
    *out = NULL;
    return E_NOINTERFACE;
}

static D3D12_COMMAND_LIST_TYPE WINAPI vlist_GetType(void *this_)
{
    return ((struct vobj *)this_)->vtbl == vproc_vtbl ? D3D12_COMMAND_LIST_TYPE_VIDEO_PROCESS
            : D3D12_COMMAND_LIST_TYPE_VIDEO_DECODE;
}

static HRESULT WINAPI vlist_Close(void *this_)
{
    struct vlist *l = this_;

    return ID3D12GraphicsCommandList_Close(l->inner);
}

static HRESULT WINAPI vlist_Reset(void *this_, ID3D12CommandAllocator *alloc)
{
    struct vlist *l = this_;

    return ID3D12GraphicsCommandList_Reset(l->inner, alloc, NULL);
}

static void WINAPI vlist_ClearState(void *this_)
{
}

static void WINAPI vlist_ResourceBarrier(void *this_, UINT count, const D3D12_RESOURCE_BARRIER *bars)
{
    D3D12_RESOURCE_BARRIER fixed[32];
    struct vlist *l = this_;
    UINT n = fix_barriers(bars, count, fixed, 32);

    if (n)
        ID3D12GraphicsCommandList_ResourceBarrier(l->inner, n, fixed);
}

static void WINAPI vlist_DiscardResource(void *this_, ID3D12Resource *res, const D3D12_DISCARD_REGION *region)
{
}

static void WINAPI vlist_noop(void *this_, void *a, void *b, void *c, void *d)
{
}

static void dump_blob(const char *tag, unsigned int n, const void *data, UINT size)
{
    char path[64];
    FILE *f;

    sprintf(path, "C:\\dxva%u.%s", n, tag);
    if ((f = fopen(path, "wb")))
    {
        fwrite(data, 1, size, f);
        fclose(f);
        LOG("  dumped %u bytes to %s\n", size, path);
    }
}

static void WINAPI vlist_DecodeFrame(void *this_, ID3D12VideoDecoder *decoder,
        const D3D12_VIDEO_DECODE_OUTPUT_STREAM_ARGUMENTS *out,
        const D3D12_VIDEO_DECODE_INPUT_STREAM_ARGUMENTS *in)
{
    unsigned int n = ++decode_calls;
    UINT i;

    LOG("DecodeFrame #%u decoder=%p heap=%p\n", n, (void *)decoder, in ? (void *)in->pHeap : NULL);
    if (out)
        LOG("  out tex=%p sub=%u convert=%d out_cs=%d dec_cs=%d\n", (void *)out->pOutputTexture2D,
            out->OutputSubresource, out->ConversionArguments.Enable,
            out->ConversionArguments.OutputColorSpace, out->ConversionArguments.DecodeColorSpace);
    if (!in)
        return;

    LOG("  bitstream buf=%p offset=%llu size=%llu, %u reference textures\n",
        (void *)in->CompressedBitstream.pBuffer,
        (unsigned long long)in->CompressedBitstream.Offset,
        (unsigned long long)in->CompressedBitstream.Size, in->ReferenceFrames.NumTexture2Ds);

    for (i = 0; i < in->NumFrameArguments && i < 10; i++)
    {
        const D3D12_VIDEO_DECODE_FRAME_ARGUMENT *a = &in->FrameArguments[i];

        LOG("  arg %u type=%d size=%u\n", i, a->Type, a->Size);
        if (a->Type == D3D12_VIDEO_DECODE_ARGUMENT_TYPE_PICTURE_PARAMETERS
                && a->Size >= sizeof(DXVA_PicParams_H264) && a->pData)
        {
            const DXVA_PicParams_H264 *pp = a->pData;

            LOG("    h264 %ux%u mbs, frame_num=%u field_pic=%u intra=%u chroma_idc=%u refs=%u "
                "cabac=%u 8x8=%u curr_idx=%u poc=%d/%d\n",
                pp->wFrameWidthInMbsMinus1 + 1, pp->wFrameHeightInMbsMinus1 + 1, pp->frame_num,
                pp->field_pic_flag, pp->IntraPicFlag, pp->chroma_format_idc, pp->num_ref_frames,
                pp->entropy_coding_mode_flag, pp->transform_8x8_mode_flag,
                pp->CurrPic.Index7Bits, pp->CurrFieldOrderCnt[0], pp->CurrFieldOrderCnt[1]);
        }
        if (a->Type == D3D12_VIDEO_DECODE_ARGUMENT_TYPE_SLICE_CONTROL && a->pData)
            LOG("    slice control: %u short slices or %u long slices\n",
                a->Size / (UINT)sizeof(DXVA_Slice_H264_Short),
                a->Size / (UINT)sizeof(DXVA_Slice_H264_Long));

        if (n <= 3 && a->pData && want_dumps())
            dump_blob(a->Type == D3D12_VIDEO_DECODE_ARGUMENT_TYPE_PICTURE_PARAMETERS ? "pp" :
                      a->Type == D3D12_VIDEO_DECODE_ARGUMENT_TYPE_SLICE_CONTROL ? "slc" : "qm",
                      n, a->pData, a->Size);
    }

    if (n <= 3 && in->CompressedBitstream.pBuffer && in->CompressedBitstream.Size && want_dumps())
    {
        BYTE *p = NULL;
        D3D12_RANGE range;

        range.Begin = 0;
        range.End = 0;
        if (SUCCEEDED(ID3D12Resource_Map(in->CompressedBitstream.pBuffer, 0, &range, (void **)&p)) && p)
        {
            dump_blob("bs", n, p + in->CompressedBitstream.Offset,
                      (UINT)in->CompressedBitstream.Size);
            ID3D12Resource_Unmap(in->CompressedBitstream.pBuffer, 0, NULL);
        }
        else LOG("  bitstream buffer would not map\n");
    }
}

static void WINAPI vlist_WriteBufferImmediate(void *this_, UINT count,
        const D3D12_WRITEBUFFERIMMEDIATE_PARAMETER *params, const D3D12_WRITEBUFFERIMMEDIATE_MODE *modes)
{
    struct vlist *l = this_;
    ID3D12GraphicsCommandList2 *list2 = NULL;

    if (SUCCEEDED(ID3D12GraphicsCommandList_QueryInterface(l->inner,
            &IID_ID3D12GraphicsCommandList2, (void **)&list2)))
    {
        ID3D12GraphicsCommandList2_WriteBufferImmediate(list2, count, params, modes);
        ID3D12GraphicsCommandList2_Release(list2);
        return;
    }
    LOG("WriteBufferImmediate dropped, no GraphicsCommandList2 underneath\n");
}

/* ---- the video processor: the colour conversion the engine actually wants ---- */

static HRESULT WINAPI vprocessor_QueryInterface(void *this_, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    if (IsEqualGUID(riid, &IID_IUnknown) || IsEqualGUID(riid, &IID_ID3D12Object)
            || IsEqualGUID(riid, &IID_ID3D12VideoProcessor))
    {
        *out = this_;
        vobj_AddRef(this_);
        return S_OK;
    }
    LOG("processor QI %s refused\n", guidstr(riid));
    *out = NULL;
    return E_NOINTERFACE;
}

static UINT WINAPI vprocessor_GetNodeMask(void *this_)
{
    return ((struct vprocessor *)this_)->node_mask;
}

static UINT WINAPI vprocessor_GetNumInputStreamDescs(void *this_)
{
    return ((struct vprocessor *)this_)->num_in;
}

static HRESULT WINAPI vprocessor_GetInputStreamDescs(void *this_, UINT count,
        D3D12_VIDEO_PROCESS_INPUT_STREAM_DESC *descs)
{
    struct vprocessor *p = this_;
    UINT i;

    if (count != p->num_in || !descs)
        return E_INVALIDARG;
    for (i = 0; i < count; i++)
        descs[i] = p->in[i];
    return S_OK;
}

static D3D12_VIDEO_PROCESS_OUTPUT_STREAM_DESC * WINAPI vprocessor_GetOutputStreamDesc(void *this_,
        D3D12_VIDEO_PROCESS_OUTPUT_STREAM_DESC *ret)
{
    *ret = ((struct vprocessor *)this_)->out;
    return ret;
}

static const char *rectstr(const D3D12_RECT *r)
{
    static char bufs[4][64];
    static unsigned int next;
    char *b = bufs[(next++) & 3];

    if (!r)
        return "(null)";
    sprintf(b, "%ld,%ld..%ld,%ld", r->left, r->top, r->right, r->bottom);
    return b;
}

static unsigned int process_calls;
static struct yuvblit blit;
static BOOL blit_ready, blit_dead;

/* if the app's output texture cannot be a render target, draw here and copy */
static ID3D12Resource *scratch;
static UINT scratch_w, scratch_h;
static DXGI_FORMAT scratch_fmt;

static void blit_log(const char *s)
{
    LOG("%s\n", s);
}

static BOOL ensure_blit(void)
{
    yuvblit_serialize_fn serialize;

    if (blit_ready)
        return TRUE;
    if (blit_dead || !real_device)
        return FALSE;
    blit_dead = TRUE;   /* one attempt: a failure here will not fix itself */
    if (!(serialize = (yuvblit_serialize_fn)real_fn("D3D12SerializeRootSignature")))
    {
        LOG("yuvblit: d3dmt.dll has no D3D12SerializeRootSignature\n");
        return FALSE;
    }
    if (FAILED(yuvblit_init(&blit, real_device, serialize, blit_log)))
        return FALSE;
    blit_dead = FALSE;
    blit_ready = TRUE;
    return TRUE;
}

static ID3D12Resource *ensure_scratch(UINT w, UINT h, DXGI_FORMAT fmt)
{
    D3D12_HEAP_PROPERTIES heap;
    D3D12_RESOURCE_DESC desc;
    ID3D12Resource *res = NULL;
    HRESULT hr;

    if (scratch && scratch_w == w && scratch_h == h && scratch_fmt == fmt)
        return scratch;
    if (scratch)
    {
        ID3D12Resource_Release(scratch);
        scratch = NULL;
    }

    memset(&heap, 0, sizeof(heap));
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    memset(&desc, 0, sizeof(desc));
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = w;
    desc.Height = h;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.Format = fmt;
    desc.SampleDesc.Count = 1;
    desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;

    hr = ((pfn_ccr)dev_orig[27])(real_device, &heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_RENDER_TARGET, NULL, &IID_ID3D12Resource, (void **)&res);
    LOG("yuvblit: scratch target %ux%u fmt=%u -> 0x%08lx\n", w, h, fmt, hr);
    if (FAILED(hr))
        return NULL;
    scratch = res;
    scratch_w = w;
    scratch_h = h;
    scratch_fmt = fmt;
    return res;
}

/* One converted frame, copied out and written to disk a few frames later so the
 * gpu has certainly finished with it. Rendering it host side is the only way to
 * check the colours without trusting an eyeball. */
static ID3D12Resource *dumpbuf;
static unsigned int dump_at, dump_pitch, dump_w, dump_h;
static BOOL dump_done;

static void dump_converted(ID3D12GraphicsCommandList *list, ID3D12Resource *tex,
        const D3D12_RESOURCE_DESC *desc, unsigned int n)
{
    D3D12_TEXTURE_COPY_LOCATION dst, src;
    D3D12_HEAP_PROPERTIES heap;
    D3D12_RESOURCE_BARRIER bar;
    D3D12_RESOURCE_DESC bd;
    HRESULT hr;

    dump_w = (UINT)desc->Width;
    dump_h = desc->Height;
    dump_pitch = (dump_w * 4 + 255) & ~255u;

    memset(&heap, 0, sizeof(heap));
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    memset(&bd, 0, sizeof(bd));
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = (UINT64)dump_pitch * dump_h;
    bd.Height = 1;
    bd.DepthOrArraySize = 1;
    bd.MipLevels = 1;
    bd.SampleDesc.Count = 1;
    bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    hr = ((pfn_ccr)dev_orig[27])(real_device, &heap, D3D12_HEAP_FLAG_NONE, &bd,
            D3D12_RESOURCE_STATE_COPY_DEST, NULL, &IID_ID3D12Resource, (void **)&dumpbuf);
    if (FAILED(hr))
    {
        LOG("frame dump: readback buffer 0x%08lx\n", hr);
        dump_done = TRUE;
        return;
    }

    memset(&bar, 0, sizeof(bar));
    bar.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    bar.Transition.pResource = tex;
    bar.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    bar.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
    bar.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    ID3D12GraphicsCommandList_ResourceBarrier(list, 1, &bar);

    memset(&dst, 0, sizeof(dst));
    memset(&src, 0, sizeof(src));
    dst.pResource = dumpbuf;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint.Footprint.Format = desc->Format;
    dst.PlacedFootprint.Footprint.Width = dump_w;
    dst.PlacedFootprint.Footprint.Height = dump_h;
    dst.PlacedFootprint.Footprint.Depth = 1;
    dst.PlacedFootprint.Footprint.RowPitch = dump_pitch;
    src.pResource = tex;
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    ID3D12GraphicsCommandList_CopyTextureRegion(list, &dst, 0, 0, 0, &src, NULL);

    bar.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
    bar.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
    ID3D12GraphicsCommandList_ResourceBarrier(list, 1, &bar);

    dump_at = n;
    LOG("frame dump: queued frame %u, %ux%u pitch %u\n", n, dump_w, dump_h, dump_pitch);
}

static void write_dump(void)
{
    BYTE *p = NULL;
    FILE *f;

    if (SUCCEEDED(ID3D12Resource_Map(dumpbuf, 0, NULL, (void **)&p)) && p)
    {
        if ((f = fopen("C:\\converted.raw", "wb")))
        {
            fwrite(p, 1, (SIZE_T)dump_pitch * dump_h, f);
            fclose(f);
            LOG("frame dump: wrote C:\\converted.raw (%ux%u, pitch %u)\n",
                dump_w, dump_h, dump_pitch);
        }
        ID3D12Resource_Unmap(dumpbuf, 0, NULL);
    }
    else LOG("frame dump: readback would not map\n");

    ID3D12Resource_Release(dumpbuf);
    dumpbuf = NULL;
    dump_done = TRUE;
}

static void WINAPI vlist_ProcessFrames(void *this_, void *processor,
        const D3D12_VIDEO_PROCESS_OUTPUT_STREAM_ARGUMENTS *out, UINT num_in,
        const D3D12_VIDEO_PROCESS_INPUT_STREAM_ARGUMENTS *in)
{
    unsigned int n = ++process_calls;
    struct vprocessor *p = processor;
    struct vlist *l = this_;
    ID3D12Resource *target;
    D3D12_RESOURCE_DESC od;
    DXGI_COLOR_SPACE_TYPE cs;
    struct nv12 *pair;
    BOOL loud = (n <= 4 || !(n % 300));
    HRESULT hr;

    if (loud)
    {
        UINT i;

        LOG("ProcessFrames #%u processor=%p streams=%u\n", n, processor, num_in);
        if (out)
            LOG("  out tex=%p sub=%u target=%s\n", (void *)out->OutputStream[0].pTexture2D,
                out->OutputStream[0].Subresource, rectstr(&out->TargetRectangle));
        for (i = 0; in && i < num_in && i < 2; i++)
            LOG("  in %u tex=%p sub=%u src=%s dst=%s orient=%d flags=0x%x past=%u future=%u\n", i,
                (void *)in[i].InputStream[0].pTexture2D, in[i].InputStream[0].Subresource,
                rectstr(&in[i].Transform.SourceRectangle),
                rectstr(&in[i].Transform.DestinationRectangle), in[i].Transform.Orientation,
                in[i].Flags, in[i].InputStream[0].ReferenceSet.NumPastFrames,
                in[i].InputStream[0].ReferenceSet.NumFutureFrames);
    }

    if (!out || !in || !num_in || !out->OutputStream[0].pTexture2D)
        return;
    if (num_in > 1 && loud)
        LOG("  only the first input stream is composited\n");

    /* the app's NV12 texture is the luma half of a pair; the chroma half is ours */
    if (!(pair = find_pair(in[0].InputStream[0].pTexture2D)))
    {
        if (loud)
            LOG("  input %p is not an NV12 pair, nothing to convert\n",
                (void *)in[0].InputStream[0].pTexture2D);
        return;
    }
    if (!ensure_blit())
        return;

    cs = override_cs((p && p->vtbl == vprocessor_vtbl && p->num_in) ? p->in[0].ColorSpace
            : DXGI_COLOR_SPACE_YCBCR_FULL_G22_LEFT_P709);
    od = res_desc(out->OutputStream[0].pTexture2D);
    target = out->OutputStream[0].pTexture2D;

    if (!(od.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET))
    {
        /* draw into our own target, then copy, leaving the app's texture in the
         * state the app believes it is in */
        D3D12_TEXTURE_COPY_LOCATION dst, src;
        D3D12_RESOURCE_BARRIER bar[2];

        if (!(target = ensure_scratch((UINT)od.Width, od.Height, od.Format)))
            return;
        hr = yuvblit_run(&blit, l->inner, pair->luma, pair->chroma, pair->width, pair->height,
                target, od.Format, cs, &in[0].Transform.SourceRectangle,
                &in[0].Transform.DestinationRectangle);
        if (FAILED(hr))
        {
            LOG("  yuvblit_run failed 0x%08lx\n", hr);
            return;
        }

        memset(bar, 0, sizeof(bar));
        bar[0].Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        bar[0].Transition.pResource = target;
        bar[0].Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        bar[0].Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        bar[0].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
        bar[1] = bar[0];
        bar[1].Transition.pResource = out->OutputStream[0].pTexture2D;
        bar[1].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
        ID3D12GraphicsCommandList_ResourceBarrier(l->inner, 2, bar);

        memset(&dst, 0, sizeof(dst));
        memset(&src, 0, sizeof(src));
        dst.pResource = out->OutputStream[0].pTexture2D;
        dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        dst.SubresourceIndex = out->OutputStream[0].Subresource;
        src.pResource = target;
        src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        ID3D12GraphicsCommandList_CopyTextureRegion(l->inner, &dst, 0, 0, 0, &src, NULL);

        bar[0].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
        bar[0].Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        bar[1].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
        bar[1].Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        ID3D12GraphicsCommandList_ResourceBarrier(l->inner, 2, bar);
        if (loud)
            LOG("  converted via scratch target, output is not rtv capable\n");
        return;
    }

    hr = yuvblit_run(&blit, l->inner, pair->luma, pair->chroma, pair->width, pair->height,
            target, od.Format, cs, &in[0].Transform.SourceRectangle,
            &in[0].Transform.DestinationRectangle);
    if (loud)
        LOG("  converted %ux%u NV12 -> fmt=%u cs=%d, 0x%08lx\n", pair->width, pair->height,
            od.Format, cs, hr);

    /* grab one frame well into playback, then write it out a little later */
    if (SUCCEEDED(hr) && !dump_done && want_dumps())
    {
        if (!dumpbuf && n == 90)
            dump_converted(l->inner, target, &od, n);
        else if (dumpbuf && n == dump_at + 30)
            write_dump();
    }
}

/* ---- the video device itself ---- */

static HRESULT WINAPI vdev_QueryInterface(void *this_, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    if (IsEqualGUID(riid, &IID_IUnknown) || IsEqualGUID(riid, &IID_ID3D12VideoDevice)
            || IsEqualGUID(riid, &IID_ID3D12VideoDevice1)
            || IsEqualGUID(riid, &IID_ID3D12VideoDevice2)
            || IsEqualGUID(riid, &IID_ID3D12VideoDevice3))
    {
        *out = this_;
        vobj_AddRef(this_);
        return S_OK;
    }
    LOG("video device QI %s refused\n", guidstr(riid));
    *out = NULL;
    return E_NOINTERFACE;
}

static HRESULT WINAPI vdev_CheckFeatureSupport(void *this_, D3D12_FEATURE_VIDEO feature,
        void *data, UINT size)
{
    switch (feature)
    {
    case D3D12_FEATURE_VIDEO_DECODE_SUPPORT:
    {
        D3D12_FEATURE_DATA_VIDEO_DECODE_SUPPORT *s = data;
        BOOL h264;

        if (size < sizeof(*s))
            return E_INVALIDARG;
        h264 = IsEqualGUID(&s->Configuration.DecodeProfile, &profile_h264);
        LOG("video CheckFeatureSupport DECODE_SUPPORT profile=%s %ux%u fmt=%u rate=%u/%u bitrate=%u\n",
            guidstr(&s->Configuration.DecodeProfile), s->Width, s->Height, s->DecodeFormat,
            s->FrameRate.Numerator, s->FrameRate.Denominator, s->BitRate);
        s->SupportFlags = D3D12_VIDEO_DECODE_SUPPORT_FLAG_NONE;
        s->ConfigurationFlags = D3D12_VIDEO_DECODE_CONFIGURATION_FLAG_NONE;
        s->DecodeTier = D3D12_VIDEO_DECODE_TIER_NOT_SUPPORTED;
        if (h264 && s->Width <= 4096 && s->Height <= 4096
                && (s->DecodeFormat == DXGI_FORMAT_NV12 || s->DecodeFormat == DXGI_FORMAT_UNKNOWN)
                && s->Configuration.InterlaceType == D3D12_VIDEO_FRAME_CODED_INTERLACE_TYPE_NONE)
        {
            s->SupportFlags = D3D12_VIDEO_DECODE_SUPPORT_FLAG_SUPPORTED;
            s->DecodeTier = D3D12_VIDEO_DECODE_TIER_2;
            LOG("  -> supported, tier 2\n");
        }
        else LOG("  -> not supported (h264=%d)\n", h264);
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_PROFILE_COUNT:
    {
        shim_DECODE_PROFILE_COUNT *c = data;

        if (size < sizeof(*c))
            return E_INVALIDARG;
        c->ProfileCount = 1;
        LOG("video CheckFeatureSupport PROFILE_COUNT -> 1\n");
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_PROFILES:
    {
        shim_DECODE_PROFILES *p = data;

        if (size < sizeof(*p))
            return E_INVALIDARG;
        LOG("video CheckFeatureSupport PROFILES count=%u\n", p->ProfileCount);
        if (p->ProfileCount < 1 || !p->pProfiles)
            return E_INVALIDARG;
        p->pProfiles[0] = profile_h264;
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_FORMAT_COUNT:
    {
        shim_DECODE_FORMAT_COUNT *c = data;

        if (size < sizeof(*c))
            return E_INVALIDARG;
        c->FormatCount = 1;
        LOG("video CheckFeatureSupport FORMAT_COUNT profile=%s -> 1\n",
            guidstr(&c->Configuration.DecodeProfile));
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_FORMATS:
    {
        shim_DECODE_FORMATS *f = data;

        if (size < sizeof(*f))
            return E_INVALIDARG;
        LOG("video CheckFeatureSupport FORMATS count=%u\n", f->FormatCount);
        if (f->FormatCount < 1 || !f->pOutputFormats)
            return E_INVALIDARG;
        f->pOutputFormats[0] = DXGI_FORMAT_NV12;
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODER_HEAP_SIZE:
    {
        shim_DECODER_HEAP_SIZE *h = data;

        if (size < sizeof(*h))
            return E_INVALIDARG;
        h->MemoryPoolL0Size = 0;
        h->MemoryPoolL1Size = 0;
        LOG("video CheckFeatureSupport HEAP_SIZE %ux%u -> 0\n",
            h->VideoDecoderHeapDesc.DecodeWidth, h->VideoDecoderHeapDesc.DecodeHeight);
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_ARCHITECTURE:
    {
        shim_VIDEO_ARCHITECTURE *a = data;

        if (size < sizeof(*a))
            return E_INVALIDARG;
        a->IOCoherent = TRUE;
        LOG("video CheckFeatureSupport ARCHITECTURE -> IOCoherent\n");
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_FEATURE_AREA_SUPPORT:
    {
        D3D12_FEATURE_DATA_VIDEO_FEATURE_AREA_SUPPORT *a = data;

        if (size < sizeof(*a))
            return E_INVALIDARG;
        a->VideoDecodeSupport = TRUE;
        a->VideoProcessSupport = FALSE;
        a->VideoEncodeSupport = FALSE;
        LOG("video CheckFeatureSupport FEATURE_AREA -> decode only\n");
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_PROCESS_SUPPORT:
    {
        D3D12_FEATURE_DATA_VIDEO_PROCESS_SUPPORT *s = data;

        if (size < sizeof(*s))
            return E_INVALIDARG;
        LOG("video CheckFeatureSupport PROCESS_SUPPORT in %ux%u fmt=%u cs=%d field=%d "
            "-> out fmt=%u cs=%d, rates %u/%u -> %u/%u\n",
            s->InputSample.Width, s->InputSample.Height, s->InputSample.Format.Format,
            s->InputSample.Format.ColorSpace, s->InputFieldType, s->OutputFormat.Format,
            s->OutputFormat.ColorSpace, s->InputFrameRate.Numerator, s->InputFrameRate.Denominator,
            s->OutputFrameRate.Numerator, s->OutputFrameRate.Denominator);

        s->SupportFlags = D3D12_VIDEO_PROCESS_SUPPORT_FLAG_SUPPORTED;
        s->ScaleSupport.OutputSizeRange.MinWidth = 1;
        s->ScaleSupport.OutputSizeRange.MinHeight = 1;
        s->ScaleSupport.OutputSizeRange.MaxWidth = 16384;
        s->ScaleSupport.OutputSizeRange.MaxHeight = 16384;
        s->ScaleSupport.Flags = D3D12_VIDEO_SCALE_SUPPORT_FLAG_NONE;
        s->FeatureSupport = 0;
        s->DeinterlaceSupport = D3D12_VIDEO_PROCESS_DEINTERLACE_FLAG_NONE;
        s->AutoProcessingSupport = 0;
        s->FilterSupport = 0;
        memset(s->FilterRangeSupport, 0, sizeof(s->FilterRangeSupport));
        LOG("  -> supported, no filters, no deinterlace\n");
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_PROCESS_MAX_INPUT_STREAMS:
    {
        shim_PROCESS_MAX_INPUT_STREAMS *m = data;

        if (size < sizeof(*m))
            return E_INVALIDARG;
        m->MaxInputStreams = 1;
        LOG("video CheckFeatureSupport MAX_INPUT_STREAMS -> 1\n");
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_PROCESS_REFERENCE_INFO:
    {
        shim_PROCESS_REFERENCE_INFO *r = data;

        if (size < sizeof(*r))
            return E_INVALIDARG;
        r->PastFrames = 0;
        r->FutureFrames = 0;
        LOG("video CheckFeatureSupport REFERENCE_INFO -> no past or future frames needed\n");
        return S_OK;
    }
    default:
        /* answering a question we do not understand with a failure is what made
         * the engine abandon the whole path once already */
        LOG("video CheckFeatureSupport feature=%d size=%u UNHANDLED, answering S_OK untouched\n",
            feature, size);
        return S_OK;
    }
}

static HRESULT WINAPI vdev_CreateVideoDecoder(void *this_, const D3D12_VIDEO_DECODER_DESC *desc,
        REFIID riid, void **out)
{
    struct vdecoder *d;

    if (!desc || !out)
        return E_INVALIDARG;
    LOG("CreateVideoDecoder profile=%s encryption=%d interlace=%d node=%u\n",
        guidstr(&desc->Configuration.DecodeProfile), desc->Configuration.BitstreamEncryption,
        desc->Configuration.InterlaceType, desc->NodeMask);
    if (!IsEqualGUID(&desc->Configuration.DecodeProfile, &profile_h264))
    {
        LOG("  -> refused, only h264 is wired up\n");
        return E_INVALIDARG;
    }
    if (!(d = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*d))))
        return E_OUTOFMEMORY;
    d->vtbl = vdecoder_vtbl;
    d->ref = 1;
    d->desc = *desc;
    *out = d;
    LOG("  -> decoder %p\n", (void *)d);
    return S_OK;
}

static HRESULT WINAPI vdev_CreateVideoDecoderHeap(void *this_,
        const D3D12_VIDEO_DECODER_HEAP_DESC *desc, REFIID riid, void **out)
{
    struct vheap *h;

    if (!desc || !out)
        return E_INVALIDARG;
    LOG("CreateVideoDecoderHeap %ux%u fmt=%u rate=%u/%u bitrate=%u dpb=%u profile=%s\n",
        desc->DecodeWidth, desc->DecodeHeight, desc->Format, desc->FrameRate.Numerator,
        desc->FrameRate.Denominator, desc->BitRate, desc->MaxDecodePictureBufferCount,
        guidstr(&desc->Configuration.DecodeProfile));
    if (!(h = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*h))))
        return E_OUTOFMEMORY;
    h->vtbl = vheap_vtbl;
    h->ref = 1;
    h->desc = *desc;
    *out = h;
    LOG("  -> heap %p\n", (void *)h);
    return S_OK;
}

static HRESULT WINAPI vdev_CreateVideoProcessor(void *this_, UINT node_mask,
        const D3D12_VIDEO_PROCESS_OUTPUT_STREAM_DESC *output_desc, UINT input_count,
        const D3D12_VIDEO_PROCESS_INPUT_STREAM_DESC *input_descs, REFIID riid, void **out)
{
    struct vprocessor *p;
    UINT i;

    if (!output_desc || !input_descs || !out || !input_count)
        return E_INVALIDARG;
    LOG("CreateVideoProcessor %u input streams, out fmt=%u cs=%d alpha=%d stereo=%d\n",
        input_count, output_desc->Format, output_desc->ColorSpace, output_desc->AlphaFillMode,
        output_desc->EnableStereo);
    for (i = 0; i < input_count && i < 2; i++)
        LOG("  in %u fmt=%u cs=%d src %ux%u..%ux%u dst %ux%u..%ux%u field=%d deint=%d "
            "past=%u future=%u alpha=%d auto=%d\n", i, input_descs[i].Format,
            input_descs[i].ColorSpace,
            input_descs[i].SourceSizeRange.MinWidth, input_descs[i].SourceSizeRange.MinHeight,
            input_descs[i].SourceSizeRange.MaxWidth, input_descs[i].SourceSizeRange.MaxHeight,
            input_descs[i].DestinationSizeRange.MinWidth, input_descs[i].DestinationSizeRange.MinHeight,
            input_descs[i].DestinationSizeRange.MaxWidth, input_descs[i].DestinationSizeRange.MaxHeight,
            input_descs[i].FieldType, input_descs[i].DeinterlaceMode,
            input_descs[i].NumPastFrames, input_descs[i].NumFutureFrames,
            input_descs[i].EnableAlphaBlending, input_descs[i].EnableAutoProcessing);

    if (input_count > 2)
    {
        LOG("  -> refused, more input streams than we can hold\n");
        return E_INVALIDARG;
    }
    if (!(p = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*p))))
        return E_OUTOFMEMORY;
    p->vtbl = vprocessor_vtbl;
    p->ref = 1;
    p->node_mask = node_mask;
    p->out = *output_desc;
    p->num_in = input_count;
    for (i = 0; i < input_count; i++)
        p->in[i] = input_descs[i];
    *out = p;
    LOG("  -> processor %p\n", (void *)p);
    return S_OK;
}

/* device2's version takes a protected resource session before the riid */
static HRESULT WINAPI vdev_CreateVideoProcessor1(void *this_, UINT node_mask,
        const D3D12_VIDEO_PROCESS_OUTPUT_STREAM_DESC *output_desc, UINT input_count,
        const D3D12_VIDEO_PROCESS_INPUT_STREAM_DESC *input_descs, void *session,
        REFIID riid, void **out)
{
    LOG("CreateVideoProcessor1 (session=%p)\n", session);
    return vdev_CreateVideoProcessor(this_, node_mask, output_desc, input_count, input_descs,
            riid, out);
}

static void init_video_vtables(void)
{
    unsigned int i;

    for (i = 0; i < 16; i++) vdev_vtbl[i] = (void *)v_notimpl;
    vdev_vtbl[0] = (void *)vdev_QueryInterface;
    vdev_vtbl[1] = (void *)vobj_AddRef;
    vdev_vtbl[2] = (void *)vobj_Release;
    vdev_vtbl[3] = (void *)vdev_CheckFeatureSupport;
    vdev_vtbl[4] = (void *)vdev_CreateVideoDecoder;
    vdev_vtbl[5] = (void *)vdev_CreateVideoDecoderHeap;
    vdev_vtbl[6] = (void *)vdev_CreateVideoProcessor;

    for (i = 0; i < 9; i++) vdecoder_vtbl[i] = (void *)v_notimpl;
    vdecoder_vtbl[0] = (void *)vdecoder_QueryInterface;
    vdecoder_vtbl[1] = (void *)vobj_AddRef;
    vdecoder_vtbl[2] = (void *)vobj_Release;
    vdecoder_vtbl[3] = (void *)vobj_GetPrivateData;
    vdecoder_vtbl[4] = (void *)vobj_SetPrivateData;
    vdecoder_vtbl[5] = (void *)vobj_SetPrivateDataInterface;
    vdecoder_vtbl[6] = (void *)vobj_SetName;
    vdecoder_vtbl[7] = (void *)vobj_GetDevice;
    vdecoder_vtbl[8] = (void *)vdecoder_GetDesc;

    memcpy(vheap_vtbl, vdecoder_vtbl, sizeof(vheap_vtbl));
    vheap_vtbl[0] = (void *)vheap_QueryInterface;
    vheap_vtbl[8] = (void *)vheap_GetDesc;

    for (i = 0; i < 23; i++) vlist_vtbl[i] = (void *)v_notimpl;
    vlist_vtbl[0] = (void *)vlist_QueryInterface;
    vlist_vtbl[1] = (void *)vobj_AddRef;
    vlist_vtbl[2] = (void *)vobj_Release;
    vlist_vtbl[3] = (void *)vobj_GetPrivateData;
    vlist_vtbl[4] = (void *)vobj_SetPrivateData;
    vlist_vtbl[5] = (void *)vobj_SetPrivateDataInterface;
    vlist_vtbl[6] = (void *)vobj_SetName;
    vlist_vtbl[7] = (void *)vobj_GetDevice;
    vlist_vtbl[8] = (void *)vlist_GetType;
    vlist_vtbl[9] = (void *)vlist_Close;
    vlist_vtbl[10] = (void *)vlist_Reset;
    vlist_vtbl[11] = (void *)vlist_ClearState;
    vlist_vtbl[12] = (void *)vlist_ResourceBarrier;
    vlist_vtbl[13] = (void *)vlist_DiscardResource;
    vlist_vtbl[14] = (void *)vlist_noop;   /* BeginQuery */
    vlist_vtbl[15] = (void *)vlist_noop;   /* EndQuery */
    vlist_vtbl[16] = (void *)vlist_noop;   /* ResolveQueryData */
    vlist_vtbl[17] = (void *)vlist_noop;   /* SetPredication */
    vlist_vtbl[18] = (void *)vlist_noop;   /* SetMarker */
    vlist_vtbl[19] = (void *)vlist_noop;   /* BeginEvent */
    vlist_vtbl[20] = (void *)vlist_noop;   /* EndEvent */
    vlist_vtbl[21] = (void *)vlist_DecodeFrame;
    vlist_vtbl[22] = (void *)vlist_WriteBufferImmediate;

    /* the process list has the same shape, ProcessFrames where DecodeFrame is */
    memcpy(vproc_vtbl, vlist_vtbl, sizeof(vproc_vtbl));
    vproc_vtbl[21] = (void *)vlist_ProcessFrames;

    for (i = 0; i < 12; i++) vprocessor_vtbl[i] = (void *)v_notimpl;
    vprocessor_vtbl[0] = (void *)vprocessor_QueryInterface;
    vprocessor_vtbl[1] = (void *)vobj_AddRef;
    vprocessor_vtbl[2] = (void *)vobj_Release;
    vprocessor_vtbl[3] = (void *)vobj_GetPrivateData;
    vprocessor_vtbl[4] = (void *)vobj_SetPrivateData;
    vprocessor_vtbl[5] = (void *)vobj_SetPrivateDataInterface;
    vprocessor_vtbl[6] = (void *)vobj_SetName;
    vprocessor_vtbl[7] = (void *)vobj_GetDevice;
    vprocessor_vtbl[8] = (void *)vprocessor_GetNodeMask;
    vprocessor_vtbl[9] = (void *)vprocessor_GetNumInputStreamDescs;
    vprocessor_vtbl[10] = (void *)vprocessor_GetInputStreamDescs;
    vprocessor_vtbl[11] = (void *)vprocessor_GetOutputStreamDesc;

    vdev_vtbl[11] = (void *)vdev_CreateVideoProcessor1;

    video_device.vtbl = vdev_vtbl;
    video_device.ref = 1;
}

/* ---- the video queue, allocator and list are DIRECT ones underneath ---- */

typedef HRESULT (WINAPI *pfn_ccq)(void *, const D3D12_COMMAND_QUEUE_DESC *, REFIID, void **);
typedef HRESULT (WINAPI *pfn_cca)(void *, D3D12_COMMAND_LIST_TYPE, REFIID, void **);
typedef void (WINAPI *pfn_ecl)(void *, UINT, ID3D12CommandList * const *);

static void *queue_vtbl[19];
static void **queue_orig;

static BOOL is_video_type(D3D12_COMMAND_LIST_TYPE type)
{
    return type == D3D12_COMMAND_LIST_TYPE_VIDEO_DECODE
            || type == D3D12_COMMAND_LIST_TYPE_VIDEO_PROCESS
            || type == D3D12_COMMAND_LIST_TYPE_VIDEO_ENCODE;
}

static void WINAPI shim_ExecuteCommandLists(void *queue, UINT count, ID3D12CommandList * const *lists)
{
    ID3D12CommandList *fixed[16];
    UINT i, swapped = 0;

    if (count <= 16)
    {
        for (i = 0; i < count; i++)
        {
            fixed[i] = lists[i];
            if (lists[i] && (*(void ***)lists[i] == (void **)vlist_vtbl
                    || *(void ***)lists[i] == (void **)vproc_vtbl))
            {
                fixed[i] = (ID3D12CommandList *)((struct vlist *)lists[i])->inner;
                swapped++;
            }
        }
        if (swapped)
        {
            DBG("ExecuteCommandLists: %u of %u video lists submitted as direct lists\n", swapped, count);
            ((pfn_ecl)queue_orig[SLOT(ID3D12CommandQueueVtbl, ExecuteCommandLists)])(queue, count, fixed);
            return;
        }
    }
    ((pfn_ecl)queue_orig[SLOT(ID3D12CommandQueueVtbl, ExecuteCommandLists)])(queue, count, lists);
}

static void wrap_queue(void *queue)
{
    void ***obj = queue;
    unsigned int i;

    if (*obj == (void **)queue_vtbl)
        return;
    if (!queue_orig)
    {
        queue_orig = *obj;
        for (i = 0; i < 19; i++)
            queue_vtbl[i] = queue_orig[i];
        queue_vtbl[SLOT(ID3D12CommandQueueVtbl, ExecuteCommandLists)] = (void *)shim_ExecuteCommandLists;
        LOG("command queue vtable wrapped\n");
    }
    if (*obj == queue_orig)
        *obj = (void **)queue_vtbl;
}

static HRESULT WINAPI shim_CreateCommandQueue(void *dev, const D3D12_COMMAND_QUEUE_DESC *desc,
        REFIID riid, void **out)
{
    UINT slot = SLOT(ID3D12DeviceVtbl, CreateCommandQueue);
    D3D12_COMMAND_QUEUE_DESC fixed;
    HRESULT hr;

    if (desc && is_video_type(desc->Type))
    {
        fixed = *desc;
        fixed.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        hr = ((pfn_ccq)dev_orig[slot])(dev, &fixed, riid, out);
        LOG("CreateCommandQueue type=%d -> direct queue 0x%08lx %p\n", desc->Type, hr,
            out ? *out : NULL);
        if (SUCCEEDED(hr) && out && *out)
            wrap_queue(*out);
        return hr;
    }
    hr = ((pfn_ccq)dev_orig[slot])(dev, desc, riid, out);
    if (SUCCEEDED(hr) && out && *out)
        wrap_queue(*out);
    return hr;
}

static HRESULT WINAPI shim_CreateCommandAllocator(void *dev, D3D12_COMMAND_LIST_TYPE type,
        REFIID riid, void **out)
{
    UINT slot = SLOT(ID3D12DeviceVtbl, CreateCommandAllocator);
    HRESULT hr;

    if (is_video_type(type))
    {
        hr = ((pfn_cca)dev_orig[slot])(dev, D3D12_COMMAND_LIST_TYPE_DIRECT, riid, out);
        LOG("CreateCommandAllocator type=%d -> direct allocator 0x%08lx %p\n", type, hr,
            out ? *out : NULL);
        return hr;
    }
    return ((pfn_cca)dev_orig[slot])(dev, type, riid, out);
}

static HRESULT create_video_list(void *dev, UINT mask, D3D12_COMMAND_LIST_TYPE type,
        void *alloc, void **out)
{
    ID3D12GraphicsCommandList *inner = NULL;
    struct vlist *l;
    HRESULT hr;

    hr = ((pfn_ccl)dev_orig[12])(dev, mask, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, NULL,
            &IID_ID3D12GraphicsCommandList, (void **)&inner);
    LOG("CreateCommandList type=%d -> inner direct list 0x%08lx %p\n", type, hr, (void *)inner);
    if (FAILED(hr))
        return hr;
    if (!(l = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*l))))
    {
        ID3D12GraphicsCommandList_Release(inner);
        return E_OUTOFMEMORY;
    }
    l->vtbl = type == D3D12_COMMAND_LIST_TYPE_VIDEO_PROCESS ? vproc_vtbl : vlist_vtbl;
    l->ref = 1;
    l->inner = inner;
    *out = l;
    LOG("  -> video list %p (%s)\n", (void *)l,
        type == D3D12_COMMAND_LIST_TYPE_VIDEO_PROCESS ? "process" : "decode");
    return S_OK;
}

typedef HRESULT (WINAPI *pfn_qi)(void *, REFIID, void **);

static HRESULT WINAPI shim_QueryInterface(void *dev, REFIID riid, void **out)
{
    HRESULT hr;

    if (riid && out && (IsEqualGUID(riid, &IID_ID3D12VideoDevice)
            || IsEqualGUID(riid, &IID_ID3D12VideoDevice1)
            || IsEqualGUID(riid, &IID_ID3D12VideoDevice2)
            || IsEqualGUID(riid, &IID_ID3D12VideoDevice3)))
    {
        *out = &video_device;
        vobj_AddRef(&video_device);
        LOG("device QI %s -> our video device %p\n", guidstr(riid), (void *)&video_device);
        return S_OK;
    }

    hr = ((pfn_qi)dev_orig[0])(dev, riid, out);
    DBG("device QI {%08lx-%04x-%04x} -> 0x%08lx%s\n", riid->Data1, riid->Data2, riid->Data3, hr,
        FAILED(hr) ? "  REFUSED" : "");
    return hr;
}

static HRESULT WINAPI shim_CheckFeatureSupport(void *dev, D3D12_FEATURE feature, void *data, UINT size)
{
    HRESULT hr = ((pfn_cfs)dev_orig[13])(dev, feature, data, size);

    if (feature == D3D12_FEATURE_FORMAT_SUPPORT && size >= sizeof(D3D12_FEATURE_DATA_FORMAT_SUPPORT))
    {
        D3D12_FEATURE_DATA_FORMAT_SUPPORT *fs = data;

        DBG("CheckFormatSupport fmt=%u -> s1=0x%08x\n", fs->Format, fs->Support1);

        /* AYUV is 4 bytes per pixel with the same layout as RGBA, so an RGBA
         * texture backs it exactly; the engine's own shader does the YUV to
         * RGB conversion. NV12 is handled by the plane pair below. */
        if ((fs->Format == DXGI_FORMAT_NV12 || fs->Format == DXGI_FORMAT_AYUV) && !fs->Support1)
        {
            fs->Support1 = D3D12_FORMAT_SUPPORT1_TEXTURE2D | D3D12_FORMAT_SUPPORT1_SHADER_LOAD
                    | D3D12_FORMAT_SUPPORT1_SHADER_SAMPLE;
            LOG("  -> claiming fmt=%u supported\n", fs->Format);
        }
    }
    return hr;
}

static HRESULT WINAPI shim_CreateCommittedResource(void *dev, const D3D12_HEAP_PROPERTIES *heap,
        D3D12_HEAP_FLAGS flags, const D3D12_RESOURCE_DESC *desc, D3D12_RESOURCE_STATES state,
        const D3D12_CLEAR_VALUE *clear, REFIID riid, void **out)
{
    D3D12_RESOURCE_DESC luma_desc, chroma_desc;
    ID3D12Resource *chroma = NULL;
    unsigned int slot;
    HRESULT hr;

    if (fix_state(state) != state)
    {
        LOG("CreateCommittedResource state 0x%x is video only, using 0x%x\n", state, fix_state(state));
        state = fix_state(state);
    }

    if (desc && desc->Format == DXGI_FORMAT_AYUV)
    {
        D3D12_RESOURCE_DESC sub = *desc;

        sub.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        hr = ((pfn_ccr)dev_orig[27])(dev, heap, flags, &sub, state, clear, riid, out);
        LOG("CreateCommittedResource AYUV %ux%u -> rgba 0x%08lx %p\n",
            (unsigned)desc->Width, desc->Height, hr, out ? *out : NULL);
        return hr;
    }

    if (!desc || desc->Format != DXGI_FORMAT_NV12)
    {
        hr = ((pfn_ccr)dev_orig[27])(dev, heap, flags, desc, state, clear, riid, out);
        /* recon: video-sized textures only, the rest is scenery */
        if (desc && desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D
                && desc->Width >= 640 && desc->Height >= 360)
            DBG("CreateTex fmt=%u %ux%u mips=%u flags=0x%x heap=%u -> %p\n", desc->Format,
                (unsigned)desc->Width, desc->Height, desc->MipLevels, desc->Flags,
                heap ? heap->Type : 0, out ? *out : NULL);
        /* the two BGRA 3840x2160 textures are the video frames: watch how the
         * engine actually gets pixels into them */
        if (want_log() && SUCCEEDED(hr) && out && *out && desc
                && (desc->Format == DXGI_FORMAT_B8G8R8A8_UNORM
                    || desc->Format == DXGI_FORMAT_R8G8B8A8_UNORM)
                && desc->Width >= 1280 && desc->MipLevels == 1
                && desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D && !desc->Flags)
            watch_resource(*out, (UINT)desc->Width, desc->Height, desc->Format);
        return hr;
    }

    luma_desc = *desc;
    luma_desc.Format = DXGI_FORMAT_R8_UNORM;
    chroma_desc = *desc;
    chroma_desc.Format = DXGI_FORMAT_R8G8_UNORM;
    chroma_desc.Width = desc->Width / 2;
    chroma_desc.Height = desc->Height / 2;

    hr = ((pfn_ccr)dev_orig[27])(dev, heap, flags, &luma_desc, state, clear, riid, out);
    LOG("CreateCommittedResource NV12 %ux%u -> luma 0x%08lx %p\n",
        (unsigned)desc->Width, desc->Height, hr, out ? *out : NULL);
    if (FAILED(hr) || !out || !*out)
        return hr;

    hr = ((pfn_ccr)dev_orig[27])(dev, heap, flags, &chroma_desc, state, clear,
            &IID_ID3D12Resource, (void **)&chroma);
    LOG("  chroma %ux%u -> 0x%08lx %p\n", (unsigned)chroma_desc.Width, chroma_desc.Height, hr, (void *)chroma);
    if (FAILED(hr))
        return S_OK;   /* luma alone still beats the game's own fallback */

    EnterCriticalSection(&cs);
    if (!pair_watch_vtbl[0])
    {
        pair_watch_vtbl[0] = (void *)pair_watch_QueryInterface;
        pair_watch_vtbl[1] = (void *)pair_watch_AddRef;
        pair_watch_vtbl[2] = (void *)pair_watch_Release;
    }
    for (slot = 0; slot < npairs; slot++)
        if (!pairs[slot].luma)
            break;
    if (slot < MAX_NV12)
    {
        pairs[slot].luma = *out;
        pairs[slot].chroma = chroma;
        pairs[slot].width = (UINT)desc->Width;
        pairs[slot].height = desc->Height;
        if (slot == npairs)
            npairs++;
        LOG("  pair %u registered\n", slot);
    }
    LeaveCriticalSection(&cs);

    if (slot < MAX_NV12)
        pair_watch_attach(*out, slot);
    else
    {
        LOG("  pair table full, chroma released\n");
        ID3D12Resource_Release(chroma);
    }
    return S_OK;
}

static HRESULT WINAPI shim_CreatePlacedResource(void *dev, void *heap, UINT64 offset,
        const D3D12_RESOURCE_DESC *desc, D3D12_RESOURCE_STATES state, const D3D12_CLEAR_VALUE *clear,
        REFIID riid, void **out)
{
    HRESULT hr = ((pfn_cpr)dev_orig[29])(dev, heap, offset, desc, fix_state(state), clear, riid, out);

    if (desc && desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D && desc->Width >= 640)
    {
        DBG("CreatePlacedTex fmt=%u %ux%u mips=%u flags=0x%x -> %p\n", desc->Format,
            (unsigned)desc->Width, desc->Height, desc->MipLevels, desc->Flags, out ? *out : NULL);
        if (want_log() && SUCCEEDED(hr) && out && *out && desc->Width >= 1280
                && desc->MipLevels == 1
                && (desc->Format == DXGI_FORMAT_B8G8R8A8_UNORM
                    || desc->Format == DXGI_FORMAT_R8G8B8A8_UNORM))
            watch_resource(*out, (UINT)desc->Width, desc->Height, desc->Format);
    }
    return hr;
}

static void WINAPI shim_CreateShaderResourceView(void *dev, ID3D12Resource *res,
        const D3D12_SHADER_RESOURCE_VIEW_DESC *desc, D3D12_CPU_DESCRIPTOR_HANDLE handle)
{
    D3D12_SHADER_RESOURCE_VIEW_DESC fixed;
    struct nv12 *pair = find_pair(res);

    if (desc && desc->Format == DXGI_FORMAT_AYUV)
    {
        fixed = *desc;
        fixed.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        DBG("CreateSRV AYUV -> rgba on %p\n", (void *)res);
        ((pfn_csrv)dev_orig[18])(dev, res, &fixed, handle);
        return;
    }

    if (pair && desc && desc->ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2D)
    {
        fixed = *desc;
        if (desc->Texture2D.PlaneSlice == 1)
        {
            fixed.Texture2D.PlaneSlice = 0;
            if (fixed.Format == DXGI_FORMAT_NV12) fixed.Format = DXGI_FORMAT_R8G8_UNORM;
            DBG("CreateSRV plane1 -> chroma %p fmt=%u\n", (void *)pair->chroma, fixed.Format);
            ((pfn_csrv)dev_orig[18])(dev, pair->chroma, &fixed, handle);
            return;
        }
        fixed.Texture2D.PlaneSlice = 0;
        if (fixed.Format == DXGI_FORMAT_NV12) fixed.Format = DXGI_FORMAT_R8_UNORM;
        DBG("CreateSRV plane0 -> luma %p fmt=%u\n", (void *)res, fixed.Format);
        ((pfn_csrv)dev_orig[18])(dev, res, &fixed, handle);
        return;
    }
    ((pfn_csrv)dev_orig[18])(dev, res, desc, handle);
}

static void WINAPI shim_GetCopyableFootprints(void *dev, const D3D12_RESOURCE_DESC *desc,
        UINT first, UINT count, UINT64 offset, D3D12_PLACED_SUBRESOURCE_FOOTPRINT *fp,
        UINT *rows, UINT64 *rowsize, UINT64 *total)
{
    UINT w, h, i, pitch;
    UINT64 at;

    if (desc && desc->Format == DXGI_FORMAT_AYUV)
    {
        D3D12_RESOURCE_DESC sub = *desc;

        sub.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        ((pfn_gcf)dev_orig[38])(dev, &sub, first, count, offset, fp, rows, rowsize, total);
        if (fp)
            for (w = 0; w < count; w++)
                fp[w].Footprint.Format = DXGI_FORMAT_AYUV;
        DBG("GetCopyableFootprints AYUV %ux%u\n", (unsigned)desc->Width, desc->Height);
        return;
    }

    if (!desc || desc->Format != DXGI_FORMAT_NV12)
    {
        ((pfn_gcf)dev_orig[38])(dev, desc, first, count, offset, fp, rows, rowsize, total);
        return;
    }

    /* windows reports two planes: R8 full size, R8G8 at half. rows are 256
     * aligned, each plane starts on a 512 boundary. */
    w = (UINT)desc->Width;
    h = desc->Height;
    at = offset;
    if (total) *total = 0;

    for (i = 0; i < count; i++)
    {
        UINT plane = first + i;
        UINT pw = plane ? w / 2 : w;
        UINT ph = plane ? h / 2 : h;
        UINT bytes = plane ? pw * 2 : pw;

        pitch = (bytes + 255) & ~255u;
        at = (at + 511) & ~511ull;

        if (fp)
        {
            fp[i].Offset = at;
            fp[i].Footprint.Format = plane ? DXGI_FORMAT_R8G8_UNORM : DXGI_FORMAT_R8_UNORM;
            fp[i].Footprint.Width = pw;
            fp[i].Footprint.Height = ph;
            fp[i].Footprint.Depth = 1;
            fp[i].Footprint.RowPitch = pitch;
        }
        if (rows) rows[i] = ph;
        if (rowsize) rowsize[i] = bytes;
        at += (UINT64)pitch * ph;
        if (total) *total = at - offset;
    }
    DBG("GetCopyableFootprints NV12 %ux%u first=%u count=%u -> total=%llu\n",
        w, h, first, count, total ? (unsigned long long)*total : 0ULL);
}

static HRESULT WINAPI shim_CreateCommandList(void *dev, UINT mask, D3D12_COMMAND_LIST_TYPE type,
        void *alloc, void *state, REFIID riid, void **out)
{
    HRESULT hr;

    if (is_video_type(type) && out)
        return create_video_list(dev, mask, type, alloc, out);

    hr = ((pfn_ccl)dev_orig[12])(dev, mask, type, alloc, state, riid, out);
    if (SUCCEEDED(hr) && out && *out)
        wrap_command_list(*out);
    return hr;
}

/* ---- command list ---- */

#define LIST_SLOTS 96
static void *list_vtbl[LIST_SLOTS];
static void **list_orig;

typedef void (WINAPI *pfn_ctr)(void *, const D3D12_TEXTURE_COPY_LOCATION *, UINT, UINT, UINT,
        const D3D12_TEXTURE_COPY_LOCATION *, const D3D12_BOX *);
typedef void (WINAPI *pfn_rb)(void *, UINT, const D3D12_RESOURCE_BARRIER *);

static void WINAPI shim_CopyTextureRegion(void *list, const D3D12_TEXTURE_COPY_LOCATION *dst,
        UINT x, UINT y, UINT z, const D3D12_TEXTURE_COPY_LOCATION *src, const D3D12_BOX *box)
{
    D3D12_TEXTURE_COPY_LOCATION fixed;
    struct nv12 *pair;

    /* dump what the engine actually staged, once. rendered host side this says
     * whether the bytes match the pitch the footprint declares. */
    if (dst && find_watched(dst->pResource) && !dumped && want_dumps()
            && src && src->Type == D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT
            && src->PlacedFootprint.Footprint.Width == 2048
            && ++videoframes == 30)
    {
        typedef HRESULT (WINAPI *pfn_m)(void *, UINT, const D3D12_RANGE *, void **);
        typedef void (WINAPI *pfn_u)(void *, UINT, const D3D12_RANGE *);
        void **vt = *(void ***)src->pResource;
        BYTE *p = NULL;

        dumped = TRUE;
        if (SUCCEEDED(((pfn_m)vt[8])(src->pResource, 0, NULL, (void **)&p)) && p)
        {
            SIZE_T bytes = (SIZE_T)src->PlacedFootprint.Footprint.RowPitch
                    * src->PlacedFootprint.Footprint.Height;
            FILE *f = fopen("C:\\videoframe.raw", "wb");

            if (f)
            {
                fwrite(p + src->PlacedFootprint.Offset, 1, bytes, f);
                fclose(f);
                LOG("dumped %llu staged bytes pitch=%u h=%u\n", (unsigned long long)bytes,
                    src->PlacedFootprint.Footprint.RowPitch, src->PlacedFootprint.Footprint.Height);
            }
            ((pfn_u)vt[9])(src->pResource, 0, NULL);
        }
        else LOG("staging buffer would not map\n");
    }

    if (dst && find_watched(dst->pResource))
        DBG("CopyTex INTO VIDEO %p type=%u sub=%u at(%u,%u) src_type=%u src_fmt=%u %ux%u pitch=%u\n",
            dst->pResource, dst->Type,
            dst->Type == D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX ? dst->SubresourceIndex : 999,
            x, y, src ? src->Type : 999,
            src && src->Type == D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT ? src->PlacedFootprint.Footprint.Format : 0,
            src && src->Type == D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT ? src->PlacedFootprint.Footprint.Width : 0,
            src && src->Type == D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT ? src->PlacedFootprint.Footprint.Height : 0,
            src && src->Type == D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT ? src->PlacedFootprint.Footprint.RowPitch : 0);

    /* recon: any copy big enough to be a video frame */
    if (src && src->Type == D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT
            && src->PlacedFootprint.Footprint.Width >= 640)
        DBG("CopyTex dst=%p sub=%u at(%u,%u) <- fp fmt=%u %ux%u pitch=%u off=%llu\n",
            dst ? dst->pResource : NULL,
            dst && dst->Type == D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX ? dst->SubresourceIndex : 999,
            x, y, src->PlacedFootprint.Footprint.Format, src->PlacedFootprint.Footprint.Width,
            src->PlacedFootprint.Footprint.Height, src->PlacedFootprint.Footprint.RowPitch,
            (unsigned long long)src->PlacedFootprint.Offset);

    if (dst && dst->Type == D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX
            && dst->SubresourceIndex == 1 && (pair = find_pair(dst->pResource)))
    {
        fixed = *dst;
        fixed.pResource = pair->chroma;
        fixed.SubresourceIndex = 0;
        DBG("CopyTextureRegion plane1 -> chroma %p\n", (void *)pair->chroma);
        ((pfn_ctr)list_orig[16])(list, &fixed, x, y, z, src, box);
        return;
    }
    ((pfn_ctr)list_orig[16])(list, dst, x, y, z, src, box);
}

static void WINAPI shim_ResourceBarrier(void *list, UINT count, const D3D12_RESOURCE_BARRIER *bars)
{
    D3D12_RESOURCE_BARRIER fixed[32];
    UINT n = fix_barriers(bars, count, fixed, 32);

    if (n)
        ((pfn_rb)list_orig[26])(list, n, fixed);
}

static void wrap_command_list(void *list)
{
    void ***obj = list;
    unsigned int i;

    if (*obj == (void **)list_vtbl)
        return;
    if (list_orig && *obj != list_orig)
        LOG("second command list vtable seen, not wrapping\n");
    if (!list_orig)
    {
        list_orig = *obj;
        for (i = 0; i < LIST_SLOTS; i++)
            list_vtbl[i] = list_orig[i];
        list_vtbl[16] = (void *)shim_CopyTextureRegion;
        list_vtbl[26] = (void *)shim_ResourceBarrier;
        LOG("command list vtable wrapped\n");
    }
    if (*obj == list_orig)
        *obj = (void **)list_vtbl;
}

static void wrap_device(void *dev)
{
    void ***obj = dev;
    unsigned int i;

    if (*obj == (void **)dev_vtbl)
        return;
    /* the numbered slots below are verified against the vtable layout; SLOT()
     * derives the rest from the headers, so this catches a mismatch at build */
    _Static_assert(SLOT(ID3D12DeviceVtbl, CheckFeatureSupport) == 13, "device vtable layout");
    _Static_assert(SLOT(ID3D12DeviceVtbl, GetCopyableFootprints) == 38, "device vtable layout");

    real_device = dev;
    init_video_vtables();

    dev_orig = *obj;
    for (i = 0; i < DEV_SLOTS; i++)
        dev_vtbl[i] = dev_orig[i];
    dev_vtbl[0] = (void *)shim_QueryInterface;
    dev_vtbl[SLOT(ID3D12DeviceVtbl, CreateCommandQueue)] = (void *)shim_CreateCommandQueue;
    dev_vtbl[SLOT(ID3D12DeviceVtbl, CreateCommandAllocator)] = (void *)shim_CreateCommandAllocator;
    dev_vtbl[12] = (void *)shim_CreateCommandList;
    dev_vtbl[13] = (void *)shim_CheckFeatureSupport;
    dev_vtbl[18] = (void *)shim_CreateShaderResourceView;
    dev_vtbl[27] = (void *)shim_CreateCommittedResource;
    dev_vtbl[29] = (void *)shim_CreatePlacedResource;
    dev_vtbl[38] = (void *)shim_GetCopyableFootprints;
    *obj = (void **)dev_vtbl;
    LOG("device %p wrapped\n", dev);
}

/* ---- exports ---- */

HRESULT WINAPI shimD3D12CreateDevice(IUnknown *adapter, D3D_FEATURE_LEVEL fl, REFIID riid, void **dev)
{
    typedef HRESULT (WINAPI *pfn)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);
    pfn fn = (pfn)real_fn("D3D12CreateDevice");
    HRESULT hr;

    if (!fn) return E_NOINTERFACE;
    hr = fn(adapter, fl, riid, dev);
    LOG("D3D12CreateDevice -> 0x%08lx\n", hr);
    if (SUCCEEDED(hr) && dev && *dev)
        wrap_device(*dev);
    return hr;
}

#define FORWARD(name) \
    __int64 WINAPI shim##name(__int64 a, __int64 b, __int64 c, __int64 d) \
    { \
        typedef __int64 (WINAPI *pfn)(__int64, __int64, __int64, __int64); \
        pfn fn = (pfn)real_fn(#name); \
        return fn ? fn(a, b, c, d) : (__int64)0x80004002; \
    }

FORWARD(D3D12GetDebugInterface)
FORWARD(D3D12SerializeRootSignature)
FORWARD(D3D12CreateRootSignatureDeserializer)
FORWARD(D3D12SerializeVersionedRootSignature)
FORWARD(D3D12CreateVersionedRootSignatureDeserializer)
FORWARD(D3D12EnableExperimentalFeatures)
FORWARD(D3D12CoreCreateLayeredDevice)
FORWARD(D3D12CoreGetLayeredDeviceSize)
FORWARD(D3D12CoreRegisterLayers)
FORWARD(GetBehaviorValue)

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, void *reserved)
{
    if (reason == DLL_PROCESS_ATTACH)
        DisableThreadLibraryCalls(inst);
    return TRUE;
}
