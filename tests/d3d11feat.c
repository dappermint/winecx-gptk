/* can a d3d11 device be created at all, and by whom?
 *
 * dxvk 2.x and 3.x refuse the device when the vulkan driver lacks a feature
 * they require, which is what makes this the test for swapping d3d11 backends.
 * the adapter description says which implementation answered.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o d3d11feat.exe d3d11feat.c -ld3d11 -ldxgi -ldxguid -luuid
 */
#define COBJMACROS
#include <windows.h>
#include <dxgi1_2.h>
#include <d3d11.h>
#include <stdio.h>

static const struct { D3D_FEATURE_LEVEL fl; const char *name; } LEVELS[] = {
    { D3D_FEATURE_LEVEL_11_1, "11_1" },
    { D3D_FEATURE_LEVEL_11_0, "11_0" },
    { D3D_FEATURE_LEVEL_10_1, "10_1" },
    { D3D_FEATURE_LEVEL_10_0, "10_0" },
    { D3D_FEATURE_LEVEL_9_3,  "9_3"  },
};

static const char *level_name(D3D_FEATURE_LEVEL fl)
{
    for (unsigned i = 0; i < ARRAYSIZE(LEVELS); i++)
        if (LEVELS[i].fl == fl) return LEVELS[i].name;
    return "?";
}

static void report_module(const char *name)
{
    HMODULE m = GetModuleHandleA(name);
    char path[MAX_PATH] = "<not loaded>";
    if (m) GetModuleFileNameA(m, path, sizeof(path));
    printf("[mod ] %-12s %s\n", name, path);
}

int main(void)
{
    IDXGIFactory1 *factory = NULL;
    HRESULT hr = CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&factory);
    if (FAILED(hr)) {
        printf("[fail] CreateDXGIFactory1 0x%08lx\n", (unsigned long)hr);
        return 1;
    }

    for (UINT i = 0; ; i++) {
        IDXGIAdapter1 *ad = NULL;
        DXGI_ADAPTER_DESC1 desc;
        if (IDXGIFactory1_EnumAdapters1(factory, i, &ad) != S_OK) break;
        if (SUCCEEDED(IDXGIAdapter1_GetDesc1(ad, &desc)))
            printf("[adap] %u: %ls (vendor 0x%04x, device 0x%04x, vram %llu MiB)\n",
                   i, desc.Description, desc.VendorId, desc.DeviceId,
                   (unsigned long long)(desc.DedicatedVideoMemory >> 20));
        IDXGIAdapter1_Release(ad);
    }

    D3D_FEATURE_LEVEL got = 0;
    ID3D11Device *dev = NULL;
    ID3D11DeviceContext *ctx = NULL;
    D3D_FEATURE_LEVEL want[] = {
        D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
        D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0,
    };

    hr = D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0,
                           want, ARRAYSIZE(want), D3D11_SDK_VERSION,
                           &dev, &got, &ctx);
    if (FAILED(hr)) {
        printf("[fail] D3D11CreateDevice 0x%08lx\n", (unsigned long)hr);
    } else {
        printf("[ ok ] D3D11CreateDevice feature level %s\n", level_name(got));

        D3D11_FEATURE_DATA_D3D11_OPTIONS o = {0};
        if (SUCCEEDED(ID3D11Device_CheckFeatureSupport(dev, D3D11_FEATURE_D3D11_OPTIONS, &o, sizeof(o))))
            printf("[opt ] map-default-no-overwrite=%d cb-partial-update=%d\n",
                   (int)o.MapNoOverwriteOnDynamicBufferSRV,
                   (int)o.ConstantBufferPartialUpdate);

        D3D11_FEATURE_DATA_THREADING t = {0};
        if (SUCCEEDED(ID3D11Device_CheckFeatureSupport(dev, D3D11_FEATURE_THREADING, &t, sizeof(t))))
            printf("[opt ] concurrent-creates=%d command-lists=%d\n",
                   (int)t.DriverConcurrentCreates, (int)t.DriverCommandLists);
    }

    report_module("d3d11.dll");
    report_module("dxgi.dll");
    report_module("d3d10core.dll");
    report_module("winevulkan.dll");
    report_module("vulkan-1.dll");

    if (ctx) ID3D11DeviceContext_Release(ctx);
    if (dev) ID3D11Device_Release(dev);
    IDXGIFactory1_Release(factory);
    return FAILED(hr) ? 1 : 0;
}
