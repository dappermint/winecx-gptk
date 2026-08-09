/* which KMTQAITYPEs does anything actually ask for, and what do we answer?
 *
 * NtGdiDdDDIQueryAdapterInfo answers a handful of types and returns
 * STATUS_NOT_IMPLEMENTED for the rest, so the honest way to decide what to
 * implement next is to find out what gets asked. This walks every type, prints
 * the status for each, and decodes the ones we claim to support. Types that
 * come back 0xc0000002 are the queue.
 *
 * mingw ships no d3dkmthk.h, so the types are declared here, matching
 * include/ddk/d3dkmthk.h in the tree.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o qaiprobe.exe qaiprobe.c -lgdi32
 */
#include <windows.h>
#include <stdio.h>

#define STATUS_SUCCESS         ((NTSTATUS)0x00000000)
#define STATUS_NOT_IMPLEMENTED ((NTSTATUS)0xc0000002)

typedef UINT D3DKMT_HANDLE;
typedef UINT D3DDDI_VIDEO_PRESENT_SOURCE_ID;

typedef enum _KMTQUERYADAPTERINFOTYPE
{
    KMTQAITYPE_UMDRIVERPRIVATE,
    KMTQAITYPE_UMDRIVERNAME,
    KMTQAITYPE_UMOPENGLINFO,
    KMTQAITYPE_GETSEGMENTSIZE,
    KMTQAITYPE_ADAPTERGUID,
    KMTQAITYPE_FLIPQUEUEINFO,
    KMTQAITYPE_ADAPTERADDRESS,
    KMTQAITYPE_SETWORKINGSETINFO,
    KMTQAITYPE_ADAPTERREGISTRYINFO,
    KMTQAITYPE_CURRENTDISPLAYMODE,
    KMTQAITYPE_MODELIST,
    KMTQAITYPE_CHECKDRIVERUPDATESTATUS,
    KMTQAITYPE_VIRTUALADDRESSINFO,
    KMTQAITYPE_DRIVERVERSION = 13,
    KMTQAITYPE_ADAPTERTYPE = 15,
    KMTQAITYPE_OUTPUTDUPLCONTEXTSCOUNT,
    KMTQAITYPE_WDDM_1_2_CAPS,
    KMTQAITYPE_UMD_DRIVER_VERSION,
    KMTQAITYPE_DIRECTFLIP_SUPPORT,
    KMTQAITYPE_MULTIPLANEOVERLAY_SUPPORT,
    KMTQAITYPE_DLIST_DRIVER_NAME,
    KMTQAITYPE_WDDM_1_3_CAPS,
    KMTQAITYPE_MULTIPLANEOVERLAY_HUD_SUPPORT,
    KMTQAITYPE_WDDM_2_0_CAPS,
    KMTQAITYPE_NODEMETADATA,
    KMTQAITYPE_CPDRIVERNAME,
    KMTQAITYPE_XBOX,
    KMTQAITYPE_INDEPENDENTFLIP_SUPPORT,
    KMTQAITYPE_MIRACASTCOMPANIONDRIVERNAME,
    KMTQAITYPE_PHYSICALADAPTERCOUNT,
    KMTQAITYPE_PHYSICALADAPTERDEVICEIDS,
    KMTQAITYPE_DRIVERCAPS_EXT,
    KMTQAITYPE_QUERY_MIRACAST_DRIVER_TYPE,
    KMTQAITYPE_QUERY_GPUMMU_CAPS,
    KMTQAITYPE_QUERY_MULTIPLANEOVERLAY_DECODE_SUPPORT,
    KMTQAITYPE_QUERY_HW_PROTECTION_TEARDOWN_COUNT,
    KMTQAITYPE_QUERY_ISBADDRIVERFORHWPROTECTIONDISABLED,
    KMTQAITYPE_MULTIPLANEOVERLAY_SECONDARY_SUPPORT,
    KMTQAITYPE_INDEPENDENTFLIP_SECONDARY_SUPPORT,
    KMTQAITYPE_PANELFITTER_SUPPORT,
    KMTQAITYPE_PHYSICALADAPTERPNPKEY,
    KMTQAITYPE_GETSEGMENTGROUPSIZE,
    KMTQAITYPE_MPO3DDI_SUPPORT,
    KMTQAITYPE_HWDRM_SUPPORT,
    KMTQAITYPE_MPOKERNELCAPS_SUPPORT,
    KMTQAITYPE_MULTIPLANEOVERLAY_STRETCH_SUPPORT,
    KMTQAITYPE_GET_DEVICE_VIDPN_OWNERSHIP_INFO,
    KMTQAITYPE_QUERYREGISTRY,
    KMTQAITYPE_KMD_DRIVER_VERSION,
    KMTQAITYPE_BLOCKLIST_KERNEL,
    KMTQAITYPE_BLOCKLIST_RUNTIME,
    KMTQAITYPE_ADAPTERGUID_RENDER,
    KMTQAITYPE_ADAPTERADDRESS_RENDER,
    KMTQAITYPE_ADAPTERREGISTRYINFO_RENDER,
    KMTQAITYPE_CHECKDRIVERUPDATESTATUS_RENDER,
    KMTQAITYPE_DRIVERVERSION_RENDER,
    KMTQAITYPE_ADAPTERTYPE_RENDER,
    KMTQAITYPE_WDDM_1_2_CAPS_RENDER,
    KMTQAITYPE_WDDM_1_3_CAPS_RENDER,
    KMTQAITYPE_QUERY_ADAPTER_UNIQUE_GUID,
    KMTQAITYPE_NODEPERFDATA,
    KMTQAITYPE_ADAPTERPERFDATA,
    KMTQAITYPE_ADAPTERPERFDATA_CAPS,
    KMTQUITYPE_GPUVERSION,
    KMTQAITYPE_DRIVER_DESCRIPTION,
    KMTQAITYPE_DRIVER_DESCRIPTION_RENDER,
    KMTQAITYPE_SCANOUT_CAPS,
    KMTQAITYPE_PARAVIRTUALIZATION_RENDER,
    KMTQAITYPE_SERVICENAME,
    KMTQAITYPE_WDDM_2_7_CAPS,
    KMTQAITYPE_DISPLAY_UMDRIVERNAME = 71,
    KMTQAITYPE_TRACKEDWORKLOAD_SUPPORT,
    KMTQAITYPE_HYBRID_DLIST_DLL_SUPPORT,
    KMTQAITYPE_DISPLAY_CAPS,
    KMTQAITYPE_WDDM_2_9_CAPS,
    KMTQAITYPE_CROSSADAPTERRESOURCE_SUPPORT,
    KMTQAITYPE_WDDM_3_0_CAPS,
    KMTQAITYPE_WSAUMDIMAGENAME,
    KMTQAITYPE_VGPUINTERFACEID,
    KMTQAITYPE_WDDM_3_1_CAPS
} KMTQUERYADAPTERINFOTYPE;

typedef struct _D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME
{
    WCHAR DeviceName[32];
    D3DKMT_HANDLE hAdapter;
    LUID AdapterLuid;
    D3DDDI_VIDEO_PRESENT_SOURCE_ID VidPnSourceId;
} D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME;

typedef struct _D3DKMT_CLOSEADAPTER
{
    D3DKMT_HANDLE hAdapter;
} D3DKMT_CLOSEADAPTER;

typedef struct _D3DKMT_QUERYADAPTERINFO
{
    D3DKMT_HANDLE           hAdapter;
    KMTQUERYADAPTERINFOTYPE Type;
    VOID                    *pPrivateDriverData;
    UINT                    PrivateDriverDataSize;
} D3DKMT_QUERYADAPTERINFO;

typedef struct _D3DKMT_SEGMENTSIZEINFO
{
    UINT64 DedicatedVideoMemorySize;
    UINT64 DedicatedSystemMemorySize;
    UINT64 SharedSystemMemorySize;
} D3DKMT_SEGMENTSIZEINFO;

typedef struct _D3DKMT_ADAPTERADDRESS
{
    UINT BusNumber;
    UINT DeviceNumber;
    UINT FunctionNumber;
} D3DKMT_ADAPTERADDRESS;

static NTSTATUS (WINAPI *pD3DKMTOpenAdapterFromGdiDisplayName)( D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME * );
static NTSTATUS (WINAPI *pD3DKMTQueryAdapterInfo)( D3DKMT_QUERYADAPTERINFO * );
static NTSTATUS (WINAPI *pD3DKMTCloseAdapter)( const D3DKMT_CLOSEADAPTER * );

struct probe
{
    KMTQUERYADAPTERINFOTYPE type;
    const char *name;
    UINT size;
};

/* size 0 means "we do not know the payload"; probed with a big scratch buffer
 * so an unimplemented type still reports its status rather than being skipped */
static const struct probe probes[] =
{
    { KMTQAITYPE_CHECKDRIVERUPDATESTATUS, "CHECKDRIVERUPDATESTATUS", sizeof(BOOL) },
    { KMTQAITYPE_DRIVERVERSION,           "DRIVERVERSION",           sizeof(UINT) },
    { KMTQAITYPE_ADAPTERTYPE,             "ADAPTERTYPE",             sizeof(UINT) },
    { KMTQAITYPE_ADAPTERGUID,             "ADAPTERGUID",             sizeof(GUID) },
    { KMTQAITYPE_ADAPTERADDRESS,          "ADAPTERADDRESS",          sizeof(D3DKMT_ADAPTERADDRESS) },
    { KMTQAITYPE_GETSEGMENTSIZE,          "GETSEGMENTSIZE",          sizeof(D3DKMT_SEGMENTSIZEINFO) },
    { KMTQAITYPE_PHYSICALADAPTERCOUNT,    "PHYSICALADAPTERCOUNT",    sizeof(UINT) },
    { KMTQAITYPE_WDDM_2_7_CAPS,           "WDDM_2_7_CAPS",           sizeof(UINT) },
    { KMTQAITYPE_WDDM_1_2_CAPS,           "WDDM_1_2_CAPS",           0 },
    { KMTQAITYPE_WDDM_1_3_CAPS,           "WDDM_1_3_CAPS",           0 },
    { KMTQAITYPE_WDDM_2_0_CAPS,           "WDDM_2_0_CAPS",           0 },
    { KMTQAITYPE_WDDM_2_9_CAPS,           "WDDM_2_9_CAPS",           0 },
    { KMTQAITYPE_WDDM_3_0_CAPS,           "WDDM_3_0_CAPS",           0 },
    { KMTQAITYPE_WDDM_3_1_CAPS,           "WDDM_3_1_CAPS",           0 },
    { KMTQAITYPE_QUERYREGISTRY,           "QUERYREGISTRY",           0 },
    { KMTQAITYPE_GETSEGMENTGROUPSIZE,     "GETSEGMENTGROUPSIZE",     0 },
    { KMTQAITYPE_ADAPTERREGISTRYINFO,     "ADAPTERREGISTRYINFO",     0 },
    { KMTQAITYPE_UMDRIVERNAME,            "UMDRIVERNAME",            0 },
    { KMTQAITYPE_NODEMETADATA,            "NODEMETADATA",            0 },
    { KMTQAITYPE_PHYSICALADAPTERDEVICEIDS,"PHYSICALADAPTERDEVICEIDS",0 },
};

int main(void)
{
    D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME open = {0};
    D3DKMT_CLOSEADAPTER close;
    unsigned char buffer[4096];
    unsigned int i, answered = 0, missing = 0;
    HMODULE gdi32;
    NTSTATUS status;

    gdi32 = LoadLibraryA( "gdi32.dll" );
    pD3DKMTOpenAdapterFromGdiDisplayName = (void *)GetProcAddress( gdi32, "D3DKMTOpenAdapterFromGdiDisplayName" );
    pD3DKMTQueryAdapterInfo = (void *)GetProcAddress( gdi32, "D3DKMTQueryAdapterInfo" );
    pD3DKMTCloseAdapter = (void *)GetProcAddress( gdi32, "D3DKMTCloseAdapter" );
    if (!pD3DKMTOpenAdapterFromGdiDisplayName || !pD3DKMTQueryAdapterInfo || !pD3DKMTCloseAdapter)
    {
        printf( "FAIL: gdi32 is missing the D3DKMT exports\n" );
        return 1;
    }

    lstrcpyW( open.DeviceName, L"\\\\.\\DISPLAY1" );
    if ((status = pD3DKMTOpenAdapterFromGdiDisplayName( &open )))
    {
        printf( "FAIL: D3DKMTOpenAdapterFromGdiDisplayName returned %08lx\n", (unsigned long)status );
        return 1;
    }
    printf( "adapter %#x, luid %08lx:%08lx\n\n", open.hAdapter,
            (unsigned long)open.AdapterLuid.HighPart, (unsigned long)open.AdapterLuid.LowPart );

    for (i = 0; i < ARRAYSIZE(probes); ++i)
    {
        D3DKMT_QUERYADAPTERINFO desc = {0};

        memset( buffer, 0xcc, sizeof(buffer) );
        desc.hAdapter = open.hAdapter;
        desc.Type = probes[i].type;
        desc.pPrivateDriverData = buffer;
        desc.PrivateDriverDataSize = probes[i].size ? probes[i].size : sizeof(buffer);

        status = pD3DKMTQueryAdapterInfo( &desc );
        printf( "%-26s type %2d  %08lx", probes[i].name, probes[i].type, (unsigned long)status );

        if (status != STATUS_SUCCESS)
        {
            printf( status == STATUS_NOT_IMPLEMENTED ? "  not implemented\n" : "\n" );
            if (status == STATUS_NOT_IMPLEMENTED) missing++;
            continue;
        }
        answered++;

        switch (probes[i].type)
        {
        case KMTQAITYPE_CHECKDRIVERUPDATESTATUS:
            printf( "  update available: %s\n", *(BOOL *)buffer ? "yes" : "no" );
            break;
        case KMTQAITYPE_DRIVERVERSION:
            printf( "  WDDM %u.%u\n", *(UINT *)buffer / 1000, (*(UINT *)buffer % 1000) / 100 );
            break;
        case KMTQAITYPE_ADAPTERTYPE:
            printf( "  bits %08x (render %d, display %d)\n", *(UINT *)buffer,
                    *(UINT *)buffer & 1, (*(UINT *)buffer >> 1) & 1 );
            break;
        case KMTQAITYPE_ADAPTERGUID:
        {
            GUID *guid = (GUID *)buffer;
            printf( "  %08lx-%04x-%04x-%02x%02x%02x%02x%02x%02x%02x%02x\n", (unsigned long)guid->Data1,
                    guid->Data2, guid->Data3, guid->Data4[0], guid->Data4[1], guid->Data4[2],
                    guid->Data4[3], guid->Data4[4], guid->Data4[5], guid->Data4[6], guid->Data4[7] );
            break;
        }
        case KMTQAITYPE_ADAPTERADDRESS:
        {
            D3DKMT_ADAPTERADDRESS *addr = (D3DKMT_ADAPTERADDRESS *)buffer;
            printf( "  bus %u device %u function %u\n", addr->BusNumber, addr->DeviceNumber, addr->FunctionNumber );
            break;
        }
        case KMTQAITYPE_GETSEGMENTSIZE:
        {
            D3DKMT_SEGMENTSIZEINFO *seg = (D3DKMT_SEGMENTSIZEINFO *)buffer;
            printf( "  dedicated video %llu MB, dedicated system %llu MB, shared %llu MB\n",
                    seg->DedicatedVideoMemorySize >> 20, seg->DedicatedSystemMemorySize >> 20,
                    seg->SharedSystemMemorySize >> 20 );
            break;
        }
        case KMTQAITYPE_PHYSICALADAPTERCOUNT:
            printf( "  %u adapter(s)\n", *(UINT *)buffer );
            break;
        case KMTQAITYPE_WDDM_2_7_CAPS:
            printf( "  caps %08x\n", *(UINT *)buffer );
            break;
        default:
            printf( "  %u bytes\n", desc.PrivateDriverDataSize );
            break;
        }
    }

    close.hAdapter = open.hAdapter;
    pD3DKMTCloseAdapter( &close );

    printf( "\n%u answered, %u not implemented\n", answered, missing );
    return 0;
}
