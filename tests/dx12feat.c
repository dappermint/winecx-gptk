/* what does apple's d3d12 actually expose, tier by tier?
 *
 * supersedes wine-debug-tools/d3d12feat.c, which stopped at D3D12_OPTIONS and
 * the shader model. A modern dx12 title does not branch on the feature level,
 * it branches on the tiers below: bindless wants resource binding tier 3, ray
 * tracing wants D3D12_RAYTRACING_TIER_1_1, and anything built against a recent
 * Agility SDK may want enhanced barriers. Any one of them coming back tier 0 is
 * a renderer path the game will refuse to take, and it fails at startup with a
 * "unsupported GPU" style message rather than a crash, which is why this needs
 * measuring before blaming the runtime.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o dx12feat.exe dx12feat.c -ld3d12 -ldxgi -ldxguid -luuid
 */
#define COBJMACROS
#include <windows.h>
#include <dxgi1_4.h>
#include <d3d12.h>
#include <stdio.h>

#define OK(hr, what) do { HRESULT _h = (hr); \
    if (FAILED(_h)) { printf("[fail] %-34s 0x%08lx\n", what, (unsigned long)_h); fflush(stdout); } \
    else { printf("[ ok ] %-34s\n", what); fflush(stdout); } } while (0)

static const struct { D3D_FEATURE_LEVEL fl; const char *name; } LEVELS[] = {
    { D3D_FEATURE_LEVEL_12_2, "12_2" },
    { D3D_FEATURE_LEVEL_12_1, "12_1" },
    { D3D_FEATURE_LEVEL_12_0, "12_0" },
    { D3D_FEATURE_LEVEL_11_1, "11_1" },
    { D3D_FEATURE_LEVEL_11_0, "11_0" },
};

static int gate_failures;

/* a tier a title branches on: report it, and say plainly whether it is enough */
static void gate(const char *name, int value, int needed, const char *what_needs_it)
{
    int ok = value >= needed;
    if (!ok) gate_failures++;
    printf("[%s] %-30s tier %d (need %d for %s)\n",
           ok ? " ok " : "GATE", name, value, needed, what_needs_it);
    fflush(stdout);
}

int main(void)
{
    IDXGIFactory4 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    ID3D12Device *dev = NULL;
    DXGI_ADAPTER_DESC1 ad;
    unsigned i;

    OK(CreateDXGIFactory1(&IID_IDXGIFactory4, (void **)&factory), "CreateDXGIFactory1");
    if (!factory) return 1;

    for (i = 0; IDXGIFactory4_EnumAdapters1(factory, i, &adapter) != DXGI_ERROR_NOT_FOUND; i++) {
        if (SUCCEEDED(IDXGIAdapter1_GetDesc1(adapter, &ad)))
            printf("[info] adapter %u: %ls  vram=%lluMB vendor=0x%04x device=0x%04x "
                   "luid=%08lx:%08lx\n",
                   i, ad.Description,
                   (unsigned long long)(ad.DedicatedVideoMemory / (1024 * 1024)),
                   ad.VendorId, ad.DeviceId,
                   (unsigned long)ad.AdapterLuid.HighPart, (unsigned long)ad.AdapterLuid.LowPart);
        IDXGIAdapter1_Release(adapter);
        adapter = NULL;
    }

    for (i = 0; i < sizeof(LEVELS) / sizeof(LEVELS[0]); i++) {
        HRESULT hr = D3D12CreateDevice(NULL, LEVELS[i].fl, &IID_ID3D12Device, (void **)&dev);
        printf("[%s] D3D12CreateDevice feature level %s%s\n",
               SUCCEEDED(hr) ? " ok " : "fail", LEVELS[i].name,
               SUCCEEDED(hr) ? "  <== highest accepted" : "");
        fflush(stdout);
        if (SUCCEEDED(hr)) break;
    }
    if (!dev) { printf("\nRESULT: no d3d12 device\n"); return 2; }

    D3D12_FEATURE_DATA_ARCHITECTURE1 arch = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_ARCHITECTURE1, &arch, sizeof(arch))))
        printf("[info] UMA %d, cache coherent UMA %d, tile based %d\n",
               (int)arch.UMA, (int)arch.CacheCoherentUMA, (int)arch.TileBasedRenderer);

    D3D12_FEATURE_DATA_SHADER_MODEL sm = { D3D_SHADER_MODEL_6_6 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_SHADER_MODEL, &sm, sizeof(sm))))
        printf("[info] highest shader model 0x%x\n", sm.HighestShaderModel);

    D3D12_FEATURE_DATA_ROOT_SIGNATURE rs = { D3D_ROOT_SIGNATURE_VERSION_1_1 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_ROOT_SIGNATURE, &rs, sizeof(rs))))
        printf("[info] highest root signature version 0x%x\n", rs.HighestVersion);

    D3D12_FEATURE_DATA_D3D12_OPTIONS o = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS, &o, sizeof(o)))) {
        printf("[info] conservative raster tier %d, rovs %d, resource heap tier %d\n",
               o.ConservativeRasterizationTier, (int)o.ROVsSupported, o.ResourceHeapTier);
        gate("resource binding", o.ResourceBindingTier, D3D12_RESOURCE_BINDING_TIER_3, "bindless");
        gate("tiled resources", o.TiledResourcesTier, D3D12_TILED_RESOURCES_TIER_2, "sparse/virtual textures");
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS1 o1 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS1, &o1, sizeof(o1))))
        printf("[info] wave ops %d (lanes %u-%u, total %u), int64 shader ops %d\n",
               (int)o1.WaveOps, o1.WaveLaneCountMin, o1.WaveLaneCountMax,
               o1.TotalLaneCount, (int)o1.Int64ShaderOps);

    D3D12_FEATURE_DATA_D3D12_OPTIONS3 o3 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS3, &o3, sizeof(o3))))
        printf("[info] view instancing tier %d, barycentrics %d, copy queue timestamps %d\n",
               o3.ViewInstancingTier, (int)o3.BarycentricsSupported,
               (int)o3.CopyQueueTimestampQueriesSupported);

    D3D12_FEATURE_DATA_D3D12_OPTIONS4 o4 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS4, &o4, sizeof(o4))))
        printf("[info] native 16-bit shader ops %d, shared resource compat tier %d\n",
               (int)o4.Native16BitShaderOpsSupported, o4.SharedResourceCompatibilityTier);

    D3D12_FEATURE_DATA_D3D12_OPTIONS5 o5 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS5, &o5, sizeof(o5)))) {
        printf("[info] render passes tier %d\n", o5.RenderPassesTier);
        gate("raytracing", o5.RaytracingTier, D3D12_RAYTRACING_TIER_1_1, "DXR / inline raytracing");
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS6 o6 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS6, &o6, sizeof(o6))))
        printf("[info] variable rate shading tier %d, additional rates %d\n",
               o6.VariableShadingRateTier, (int)o6.AdditionalShadingRatesSupported);

    D3D12_FEATURE_DATA_D3D12_OPTIONS7 o7 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS7, &o7, sizeof(o7)))) {
        gate("mesh shader", o7.MeshShaderTier, D3D12_MESH_SHADER_TIER_1, "mesh/amplification shaders");
        gate("sampler feedback", o7.SamplerFeedbackTier, D3D12_SAMPLER_FEEDBACK_TIER_0_9, "texture streaming feedback");
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS9 o9 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS9, &o9, sizeof(o9))))
        printf("[info] atomic int64 on typed resource %d, on group shared %d\n",
               (int)o9.AtomicInt64OnTypedResourceSupported,
               (int)o9.AtomicInt64OnGroupSharedSupported);

    D3D12_FEATURE_DATA_D3D12_OPTIONS12 o12 = { 0 };
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(dev, D3D12_FEATURE_D3D12_OPTIONS12, &o12, sizeof(o12)))) {
        printf("[%s] %-30s %d (need 1 for Agility SDK titles that opt in)\n",
               o12.EnhancedBarriersSupported ? " ok " : "GATE", "enhanced barriers",
               (int)o12.EnhancedBarriersSupported);
        if (!o12.EnhancedBarriersSupported) gate_failures++;
    }
    else printf("[info] OPTIONS12 not answered at all (pre-enhanced-barriers runtime)\n");

    /* a title that gets this far can submit work: queue, allocator, list, fence */
    ID3D12CommandQueue *queue = NULL;
    ID3D12CommandAllocator *alloc = NULL;
    ID3D12GraphicsCommandList *list = NULL;
    ID3D12Fence *fence = NULL;
    D3D12_COMMAND_QUEUE_DESC qd = { D3D12_COMMAND_LIST_TYPE_DIRECT, 0, D3D12_COMMAND_QUEUE_FLAG_NONE, 0 };

    OK(ID3D12Device_CreateCommandQueue(dev, &qd, &IID_ID3D12CommandQueue, (void **)&queue),
       "CreateCommandQueue");
    OK(ID3D12Device_CreateCommandAllocator(dev, D3D12_COMMAND_LIST_TYPE_DIRECT,
       &IID_ID3D12CommandAllocator, (void **)&alloc), "CreateCommandAllocator");
    if (alloc)
        OK(ID3D12Device_CreateCommandList(dev, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, NULL,
           &IID_ID3D12GraphicsCommandList, (void **)&list), "CreateCommandList");
    OK(ID3D12Device_CreateFence(dev, 0, D3D12_FENCE_FLAG_NONE, &IID_ID3D12Fence, (void **)&fence),
       "CreateFence");

    if (list && queue) {
        ID3D12GraphicsCommandList_Close(list);
        ID3D12CommandList *lists[] = { (ID3D12CommandList *)list };
        ID3D12CommandQueue_ExecuteCommandLists(queue, 1, lists);
        OK(ID3D12CommandQueue_Signal(queue, fence, 1), "ExecuteCommandLists + Signal");
        printf("[info] fence after submit: %llu\n",
               (unsigned long long)ID3D12Fence_GetCompletedValue(fence));
    }

    printf("\nRESULT: d3d12 device up, %d gate(s) below what a modern dx12 title asks for\n",
           gate_failures);
    return gate_failures ? 3 : 0;
}
