/* dxgishim: a truthful driver version for the dxgi.dll builtin slot.
 *
 * D3DMetal's IDXGIAdapter::CheckInterfaceSupport returns S_OK and writes -1
 * into the version out-param. Engines format that LARGE_INTEGER as four words
 * and get "65535.65535.65535.65535", which fails every minimum-driver check:
 * Helldivers 2 puts a modal "GPU drivers are out of date" box in front of the
 * game for it.
 *
 * It has to live here rather than in the d3d12 shim. Measured on Helldivers 2:
 * neither helldivers2.exe nor game.dll imports d3d12 or dxgi statically, both
 * are loaded at runtime, and the driver check happens before d3d12 is touched
 * at all. So there is no import entry to patch and no earlier hook to take.
 *
 * Apple's dxgi.dll is renamed dxgm.dll beside this one, with its PE export
 * directory rewritten to match; see rename-pe-export.py for why the filename
 * alone is not enough.
 */
#define COBJMACROS
#include <windows.h>
#include <dxgi1_4.h>
#include <stdio.h>

static HMODULE real_mod;
static FILE *logf;

#define LOG(...) do { if (logf) { fprintf(logf, __VA_ARGS__); fflush(logf); } } while (0)

static void *real_fn(const char *name)
{
    if (!real_mod)
    {
        logf = fopen("C:\\dxgishim.log", "a");
        real_mod = LoadLibraryA("dxgm.dll");
        LOG("=== shim up: dxgm.dll=%p err=%lu\n", (void *)real_mod, GetLastError());
    }
    return real_mod ? (void *)GetProcAddress(real_mod, name) : NULL;
}

/* ---- the driver version ---- */

/* What wine already publishes for this adapter in
 * HKLM\System\CurrentControlSet\Control\Class\{4d36e968-...}\0000\DriverVersion,
 * so the two agree instead of being separately invented. */
#define UMD_HIGH ((35u << 16) | 0u)
#define UMD_LOW  ((15u << 16) | 6094u)

typedef HRESULT (WINAPI *pfn_cis)(void *, REFGUID, LARGE_INTEGER *);
static pfn_cis real_cis;

static HRESULT WINAPI shim_CheckInterfaceSupport(void *adapter, REFGUID guid, LARGE_INTEGER *umd)
{
    HRESULT hr = real_cis(adapter, guid, umd);

    if (SUCCEEDED(hr) && umd && umd->QuadPart == -1)
    {
        umd->HighPart = UMD_HIGH;
        umd->LowPart = UMD_LOW;
    }
    return hr;
}

/* Patches the slot in the vtable itself rather than swapping any one object's
 * vtable pointer. Every adapter D3DMetal hands out shares this table, so one
 * write covers adapters obtained through EnumAdapters, EnumAdapters1,
 * EnumAdapterByLuid and EnumAdapterByGpuPreference alike. Wrapping the factory
 * would only have covered the two we thought to wrap. */
#define CIS_SLOT 9

static void patch_adapter_vtable(IUnknown *factory_unk)
{
    static BOOL done;
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter *adapter = NULL;
    DWORD old_prot;
    void **vt;

    if (done || !factory_unk)
        return;
    done = TRUE;

    if (FAILED(IUnknown_QueryInterface(factory_unk, &IID_IDXGIFactory1, (void **)&factory)))
    {
        LOG("factory is not an IDXGIFactory1, adapter left alone\n");
        return;
    }
    if (IDXGIFactory1_EnumAdapters(factory, 0, &adapter) != S_OK || !adapter)
    {
        LOG("no adapter 0 to read a vtable from\n");
        IDXGIFactory1_Release(factory);
        return;
    }

    vt = *(void ***)adapter;
    real_cis = (pfn_cis)vt[CIS_SLOT];
    if (VirtualProtect(&vt[CIS_SLOT], sizeof(void *), PAGE_READWRITE, &old_prot))
    {
        vt[CIS_SLOT] = (void *)shim_CheckInterfaceSupport;
        VirtualProtect(&vt[CIS_SLOT], sizeof(void *), old_prot, &old_prot);
        LOG("adapter vtable patched, CheckInterfaceSupport answers 35.0.15.6094\n");
    }
    else
    {
        real_cis = NULL;
        LOG("VirtualProtect on the adapter vtable failed, %lu\n", GetLastError());
    }

    IDXGIAdapter_Release(adapter);
    IDXGIFactory1_Release(factory);
}

/* ---- exports ---- */

HRESULT WINAPI shimCreateDXGIFactory(REFIID riid, void **out)
{
    typedef HRESULT (WINAPI *pfn)(REFIID, void **);
    pfn fn = (pfn)real_fn("CreateDXGIFactory");
    HRESULT hr;

    if (!fn) return E_NOINTERFACE;
    hr = fn(riid, out);
    if (SUCCEEDED(hr) && out && *out)
        patch_adapter_vtable((IUnknown *)*out);
    return hr;
}

HRESULT WINAPI shimCreateDXGIFactory1(REFIID riid, void **out)
{
    typedef HRESULT (WINAPI *pfn)(REFIID, void **);
    pfn fn = (pfn)real_fn("CreateDXGIFactory1");
    HRESULT hr;

    if (!fn) return E_NOINTERFACE;
    hr = fn(riid, out);
    if (SUCCEEDED(hr) && out && *out)
        patch_adapter_vtable((IUnknown *)*out);
    return hr;
}

HRESULT WINAPI shimCreateDXGIFactory2(UINT flags, REFIID riid, void **out)
{
    typedef HRESULT (WINAPI *pfn)(UINT, REFIID, void **);
    pfn fn = (pfn)real_fn("CreateDXGIFactory2");
    HRESULT hr;

    if (!fn) return E_NOINTERFACE;
    hr = fn(flags, riid, out);
    if (SUCCEEDED(hr) && out && *out)
        patch_adapter_vtable((IUnknown *)*out);
    return hr;
}

#define FORWARD(name) \
    __int64 WINAPI shim##name(__int64 a, __int64 b, __int64 c, __int64 d) \
    { \
        typedef __int64 (WINAPI *pfn)(__int64, __int64, __int64, __int64); \
        pfn fn = (pfn)real_fn(#name); \
        return fn ? fn(a, b, c, d) : (__int64)0x80004002; \
    }

FORWARD(DXGID3D10CreateDevice)
FORWARD(DXGID3D10RegisterLayers)
FORWARD(DXGIGetDebugInterface1)
FORWARD(DXGIDeclareAdapterRemovalSupport)

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, void *reserved)
{
    if (reason == DLL_PROCESS_ATTACH)
        DisableThreadLibraryCalls(inst);
    return TRUE;
}
