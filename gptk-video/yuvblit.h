/* yuvblit: the colour conversion a d3d12 video processor owes the app.
 *
 * D3DMetal has no video processor, so an app that decodes to NV12 and asks
 * d3d12 to convert has nowhere to go. This is that conversion as an ordinary
 * graphics pass: a fullscreen triangle samples the luma and chroma planes and
 * writes RGB, which is all ProcessFrames does for a single unfiltered stream.
 *
 * NV12 arrives as two real textures because this stack cannot sample the
 * planar format: R8 at full size and R8G8 at half. Sampling them separately is
 * what the hardware does internally anyway.
 *
 * Shared by the shim and by yuvtest.exe, so the maths can be checked against a
 * cpu reference without launching a game.
 */
#ifndef YUVBLIT_H
#define YUVBLIT_H

#define YUVBLIT_SRVS 128   /* two per call, so 64 calls of slack */
#define YUVBLIT_RTVS 16
#define YUVBLIT_PSOS 4

typedef HRESULT (WINAPI *yuvblit_serialize_fn)(const D3D12_ROOT_SIGNATURE_DESC *,
        D3D_ROOT_SIGNATURE_VERSION, ID3DBlob **, ID3DBlob **);

struct yuvblit
{
    ID3D12Device *dev;
    ID3D12RootSignature *rootsig;
    ID3D12DescriptorHeap *srv_heap;
    ID3D12DescriptorHeap *rtv_heap;
    UINT srv_size, rtv_size;
    UINT next_srv, next_rtv;
    ID3DBlob *vs, *ps;
    struct { DXGI_FORMAT fmt; ID3D12PipelineState *pso; } psos[YUVBLIT_PSOS];
    void (*log)(const char *);
    char msg[256];
};

static const char yuvblit_hlsl[] =
"cbuffer params : register(b0)\n"
"{\n"
"    float4 m0, m1, m2, uvxf;\n"
"};\n"
"Texture2D<float4> luma : register(t0);\n"
"Texture2D<float4> chroma : register(t1);\n"
"SamplerState smp : register(s0);\n"
"\n"
"struct vs_out { float4 pos : SV_Position; float2 uv : TEXCOORD0; };\n"
"\n"
"vs_out vs_main(uint id : SV_VertexID)\n"
"{\n"
"    vs_out o;\n"
"    float2 p = float2((id << 1) & 2, id & 2);\n"
"    o.uv = p;\n"   /* uv reaches 1 at clip +1, the triangle runs past to 2 */
"    o.pos = float4(p.x * 2.0 - 1.0, 1.0 - p.y * 2.0, 0.0, 1.0);\n"
"    return o;\n"
"}\n"
"\n"
"float4 ps_main(vs_out i) : SV_Target\n"
"{\n"
"    float2 t = i.uv * uvxf.xy + uvxf.zw;\n"
"    float3 yuv;\n"
"    yuv.x = luma.Sample(smp, t).x;\n"
"    yuv.yz = chroma.Sample(smp, t).xy;\n"
"    float3 rgb;\n"
"    rgb.r = dot(m0.xyz, yuv) + m0.w;\n"
"    rgb.g = dot(m1.xyz, yuv) + m1.w;\n"
"    rgb.b = dot(m2.xyz, yuv) + m2.w;\n"
"    return float4(saturate(rgb), 1.0);\n"
"}\n";

/* these two return a struct by value, which the C bindings will not call
 * directly; go through the vtable with the hidden return pointer instead */
static D3D12_CPU_DESCRIPTOR_HANDLE yuvblit_cpu_start(ID3D12DescriptorHeap *heap)
{
    typedef D3D12_CPU_DESCRIPTOR_HANDLE * (WINAPI *pfn)(ID3D12DescriptorHeap *,
            D3D12_CPU_DESCRIPTOR_HANDLE *);
    D3D12_CPU_DESCRIPTOR_HANDLE ret = { 0 };

    ((pfn)heap->lpVtbl->GetCPUDescriptorHandleForHeapStart)(heap, &ret);
    return ret;
}

static D3D12_GPU_DESCRIPTOR_HANDLE yuvblit_gpu_start(ID3D12DescriptorHeap *heap)
{
    typedef D3D12_GPU_DESCRIPTOR_HANDLE * (WINAPI *pfn)(ID3D12DescriptorHeap *,
            D3D12_GPU_DESCRIPTOR_HANDLE *);
    D3D12_GPU_DESCRIPTOR_HANDLE ret = { 0 };

    ((pfn)heap->lpVtbl->GetGPUDescriptorHandleForHeapStart)(heap, &ret);
    return ret;
}

static void yuvblit_say(struct yuvblit *b, const char *fmt, ...)
{
    va_list args;

    if (!b->log)
        return;
    va_start(args, fmt);
    vsnprintf(b->msg, sizeof(b->msg), fmt, args);
    va_end(args);
    b->log(b->msg);
}

/* The colour space the app declares decides the matrix. Rows are
 * (coefficients for y, u, v, then the constant), so the shader is three dots. */
static void yuvblit_matrix(DXGI_COLOR_SPACE_TYPE cs, float m[12])
{
    /* p601: 6 studio, 7 full. p709: 5 full x601, 8 studio, 9 full */
    BOOL studio = (cs == 6 || cs == 8 || cs == 10 || cs == 12);
    BOOL p601 = (cs == 6 || cs == 7);
    float kr = p601 ? 0.299f : 0.2126f;
    float kb = p601 ? 0.114f : 0.0722f;
    float kg = 1.0f - kr - kb;
    float yscale = 1.0f, cscale = 1.0f, yoff = 0.0f;
    float rv, gu, gv, bu;
    UINT i;

    if (studio)
    {
        yscale = 255.0f / 219.0f;
        cscale = 255.0f / 224.0f;
        yoff = 16.0f / 255.0f;
    }

    rv = 2.0f * (1.0f - kr) * cscale;
    bu = 2.0f * (1.0f - kb) * cscale;
    gu = -2.0f * kb * (1.0f - kb) / kg * cscale;
    gv = -2.0f * kr * (1.0f - kr) / kg * cscale;

    m[0] = yscale;  m[1] = 0.0f; m[2] = rv;   m[3] = 0.0f;
    m[4] = yscale;  m[5] = gu;   m[6] = gv;   m[7] = 0.0f;
    m[8] = yscale;  m[9] = bu;   m[10] = 0.0f; m[11] = 0.0f;

    /* fold the luma and chroma offsets into the constant column */
    for (i = 0; i < 3; i++)
        m[i * 4 + 3] = -yscale * yoff - 0.5f * (m[i * 4 + 1] + m[i * 4 + 2]);
}

static void yuvblit_release(struct yuvblit *b)
{
    UINT i;

    for (i = 0; i < YUVBLIT_PSOS; i++)
        if (b->psos[i].pso)
        {
            ID3D12PipelineState_Release(b->psos[i].pso);
            b->psos[i].pso = NULL;
        }
    if (b->vs) { ID3D10Blob_Release(b->vs); b->vs = NULL; }
    if (b->ps) { ID3D10Blob_Release(b->ps); b->ps = NULL; }
    if (b->rootsig) { ID3D12RootSignature_Release(b->rootsig); b->rootsig = NULL; }
    if (b->srv_heap) { ID3D12DescriptorHeap_Release(b->srv_heap); b->srv_heap = NULL; }
    if (b->rtv_heap) { ID3D12DescriptorHeap_Release(b->rtv_heap); b->rtv_heap = NULL; }
}

static HRESULT yuvblit_compile(struct yuvblit *b)
{
    HRESULT (WINAPI *compile)(const void *, SIZE_T, const char *, const void *, void *,
            const char *, const char *, UINT, UINT, ID3DBlob **, ID3DBlob **);
    ID3DBlob *errors = NULL;
    HMODULE mod;
    HRESULT hr;

    if (!(mod = LoadLibraryA("d3dcompiler_47.dll")))
    {
        yuvblit_say(b, "yuvblit: no d3dcompiler_47.dll (err %lu)", GetLastError());
        return E_FAIL;
    }
    if (!(compile = (void *)GetProcAddress(mod, "D3DCompile")))
    {
        yuvblit_say(b, "yuvblit: d3dcompiler_47 has no D3DCompile");
        return E_FAIL;
    }

    hr = compile(yuvblit_hlsl, sizeof(yuvblit_hlsl) - 1, "yuvblit", NULL, NULL, "vs_main",
            "vs_5_0", 0, 0, &b->vs, &errors);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: vs failed 0x%08lx: %s", hr,
                errors ? (char *)ID3D10Blob_GetBufferPointer(errors) : "no message");
        if (errors) ID3D10Blob_Release(errors);
        return hr;
    }
    hr = compile(yuvblit_hlsl, sizeof(yuvblit_hlsl) - 1, "yuvblit", NULL, NULL, "ps_main",
            "ps_5_0", 0, 0, &b->ps, &errors);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: ps failed 0x%08lx: %s", hr,
                errors ? (char *)ID3D10Blob_GetBufferPointer(errors) : "no message");
        if (errors) ID3D10Blob_Release(errors);
        return hr;
    }
    if (errors) ID3D10Blob_Release(errors);
    yuvblit_say(b, "yuvblit: shaders compiled, vs %u bytes ps %u bytes",
            (UINT)ID3D10Blob_GetBufferSize(b->vs), (UINT)ID3D10Blob_GetBufferSize(b->ps));
    return S_OK;
}

static HRESULT yuvblit_init(struct yuvblit *b, ID3D12Device *dev, yuvblit_serialize_fn serialize,
        void (*log)(const char *))
{
    D3D12_DESCRIPTOR_RANGE range;
    D3D12_ROOT_PARAMETER params[2];
    D3D12_STATIC_SAMPLER_DESC sampler;
    D3D12_ROOT_SIGNATURE_DESC rs;
    D3D12_DESCRIPTOR_HEAP_DESC hd;
    ID3DBlob *blob = NULL, *errors = NULL;
    HRESULT hr;

    memset(b, 0, sizeof(*b));
    b->dev = dev;
    b->log = log;

    if (FAILED(hr = yuvblit_compile(b)))
        return hr;

    memset(&range, 0, sizeof(range));
    range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    range.NumDescriptors = 2;
    range.BaseShaderRegister = 0;

    memset(params, 0, sizeof(params));
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants.Num32BitValues = 16;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[1].DescriptorTable.NumDescriptorRanges = 1;
    params[1].DescriptorTable.pDescriptorRanges = &range;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;

    memset(&sampler, 0, sizeof(sampler));
    sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.AddressV = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.MaxLOD = D3D12_FLOAT32_MAX;
    sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;

    memset(&rs, 0, sizeof(rs));
    rs.NumParameters = 2;
    rs.pParameters = params;
    rs.NumStaticSamplers = 1;
    rs.pStaticSamplers = &sampler;

    hr = serialize(&rs, D3D_ROOT_SIGNATURE_VERSION_1, &blob, &errors);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: root signature 0x%08lx: %s", hr,
                errors ? (char *)ID3D10Blob_GetBufferPointer(errors) : "no message");
        if (errors) ID3D10Blob_Release(errors);
        yuvblit_release(b);
        return hr;
    }
    if (errors) ID3D10Blob_Release(errors);
    hr = ID3D12Device_CreateRootSignature(dev, 0, ID3D10Blob_GetBufferPointer(blob),
            ID3D10Blob_GetBufferSize(blob), &IID_ID3D12RootSignature, (void **)&b->rootsig);
    ID3D10Blob_Release(blob);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: CreateRootSignature 0x%08lx", hr);
        yuvblit_release(b);
        return hr;
    }

    memset(&hd, 0, sizeof(hd));
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = YUVBLIT_SRVS;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr = ID3D12Device_CreateDescriptorHeap(dev, &hd, &IID_ID3D12DescriptorHeap,
            (void **)&b->srv_heap);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: srv heap 0x%08lx", hr);
        yuvblit_release(b);
        return hr;
    }

    memset(&hd, 0, sizeof(hd));
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    hd.NumDescriptors = YUVBLIT_RTVS;
    hr = ID3D12Device_CreateDescriptorHeap(dev, &hd, &IID_ID3D12DescriptorHeap,
            (void **)&b->rtv_heap);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: rtv heap 0x%08lx", hr);
        yuvblit_release(b);
        return hr;
    }

    b->srv_size = ID3D12Device_GetDescriptorHandleIncrementSize(dev,
            D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    b->rtv_size = ID3D12Device_GetDescriptorHandleIncrementSize(dev,
            D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    yuvblit_say(b, "yuvblit: ready (srv stride %u, rtv stride %u)", b->srv_size, b->rtv_size);
    return S_OK;
}

static ID3D12PipelineState *yuvblit_pso(struct yuvblit *b, DXGI_FORMAT fmt)
{
    D3D12_GRAPHICS_PIPELINE_STATE_DESC pd;
    ID3D12PipelineState *pso = NULL;
    HRESULT hr;
    UINT i;

    for (i = 0; i < YUVBLIT_PSOS; i++)
        if (b->psos[i].pso && b->psos[i].fmt == fmt)
            return b->psos[i].pso;

    memset(&pd, 0, sizeof(pd));
    pd.pRootSignature = b->rootsig;
    pd.VS.pShaderBytecode = ID3D10Blob_GetBufferPointer(b->vs);
    pd.VS.BytecodeLength = ID3D10Blob_GetBufferSize(b->vs);
    pd.PS.pShaderBytecode = ID3D10Blob_GetBufferPointer(b->ps);
    pd.PS.BytecodeLength = ID3D10Blob_GetBufferSize(b->ps);
    pd.SampleMask = ~0u;
    pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pd.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    pd.RasterizerState.DepthClipEnable = TRUE;
    pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    pd.NumRenderTargets = 1;
    pd.RTVFormats[0] = fmt;
    pd.DSVFormat = DXGI_FORMAT_UNKNOWN;
    pd.SampleDesc.Count = 1;

    hr = ID3D12Device_CreateGraphicsPipelineState(b->dev, &pd, &IID_ID3D12PipelineState,
            (void **)&pso);
    if (FAILED(hr))
    {
        yuvblit_say(b, "yuvblit: pso for format %u failed 0x%08lx", fmt, hr);
        return NULL;
    }
    for (i = 0; i < YUVBLIT_PSOS; i++)
        if (!b->psos[i].pso)
        {
            b->psos[i].fmt = fmt;
            b->psos[i].pso = pso;
            yuvblit_say(b, "yuvblit: pso ready for format %u", fmt);
            return pso;
        }
    yuvblit_say(b, "yuvblit: pso table full, leaking one for format %u", fmt);
    return pso;
}

/* Records the conversion. The caller owns the resource states: luma and chroma
 * must be readable by a pixel shader and dst must be a render target. */
static HRESULT yuvblit_run(struct yuvblit *b, ID3D12GraphicsCommandList *list,
        ID3D12Resource *luma, ID3D12Resource *chroma, UINT src_w, UINT src_h,
        ID3D12Resource *dst, DXGI_FORMAT dst_fmt, DXGI_COLOR_SPACE_TYPE cs,
        const D3D12_RECT *src_rect, const D3D12_RECT *dst_rect)
{
    D3D12_CPU_DESCRIPTOR_HANDLE srv_cpu, rtv_cpu;
    D3D12_GPU_DESCRIPTOR_HANDLE srv_gpu;
    D3D12_SHADER_RESOURCE_VIEW_DESC sd;
    D3D12_RENDER_TARGET_VIEW_DESC rd;
    ID3D12PipelineState *pso;
    D3D12_VIEWPORT vp;
    D3D12_RECT scissor;
    float consts[16];
    UINT srv, rtv;

    if (!b->rootsig || !luma || !chroma || !dst || !list)
        return E_INVALIDARG;
    if (!(pso = yuvblit_pso(b, dst_fmt)))
        return E_FAIL;

    srv = b->next_srv;
    b->next_srv = (b->next_srv + 2) % YUVBLIT_SRVS;
    rtv = b->next_rtv;
    b->next_rtv = (b->next_rtv + 1) % YUVBLIT_RTVS;

    srv_cpu = yuvblit_cpu_start(b->srv_heap);
    srv_gpu = yuvblit_gpu_start(b->srv_heap);
    srv_cpu.ptr += (SIZE_T)srv * b->srv_size;
    srv_gpu.ptr += (UINT64)srv * b->srv_size;
    rtv_cpu = yuvblit_cpu_start(b->rtv_heap);
    rtv_cpu.ptr += (SIZE_T)rtv * b->rtv_size;

    memset(&sd, 0, sizeof(sd));
    sd.Format = DXGI_FORMAT_R8_UNORM;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    ID3D12Device_CreateShaderResourceView(b->dev, luma, &sd, srv_cpu);

    sd.Format = DXGI_FORMAT_R8G8_UNORM;
    srv_cpu.ptr += b->srv_size;
    ID3D12Device_CreateShaderResourceView(b->dev, chroma, &sd, srv_cpu);

    memset(&rd, 0, sizeof(rd));
    rd.Format = dst_fmt;
    rd.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
    ID3D12Device_CreateRenderTargetView(b->dev, dst, &rd, rtv_cpu);

    yuvblit_matrix(cs, consts);
    consts[12] = 1.0f;
    consts[13] = 1.0f;
    consts[14] = 0.0f;
    consts[15] = 0.0f;
    if (src_rect && src_w && src_h && (src_rect->right - src_rect->left) > 0)
    {
        consts[12] = (float)(src_rect->right - src_rect->left) / (float)src_w;
        consts[13] = (float)(src_rect->bottom - src_rect->top) / (float)src_h;
        consts[14] = (float)src_rect->left / (float)src_w;
        consts[15] = (float)src_rect->top / (float)src_h;
    }

    memset(&vp, 0, sizeof(vp));
    if (dst_rect)
    {
        vp.TopLeftX = (FLOAT)dst_rect->left;
        vp.TopLeftY = (FLOAT)dst_rect->top;
        vp.Width = (FLOAT)(dst_rect->right - dst_rect->left);
        vp.Height = (FLOAT)(dst_rect->bottom - dst_rect->top);
        scissor = *dst_rect;
    }
    else
    {
        vp.Width = (FLOAT)src_w;
        vp.Height = (FLOAT)src_h;
        scissor.left = 0;
        scissor.top = 0;
        scissor.right = src_w;
        scissor.bottom = src_h;
    }
    vp.MaxDepth = 1.0f;

    ID3D12GraphicsCommandList_SetDescriptorHeaps(list, 1, &b->srv_heap);
    ID3D12GraphicsCommandList_SetGraphicsRootSignature(list, b->rootsig);
    ID3D12GraphicsCommandList_SetPipelineState(list, pso);
    ID3D12GraphicsCommandList_SetGraphicsRoot32BitConstants(list, 0, 16, consts, 0);
    ID3D12GraphicsCommandList_SetGraphicsRootDescriptorTable(list, 1, srv_gpu);
    ID3D12GraphicsCommandList_OMSetRenderTargets(list, 1, &rtv_cpu, FALSE, NULL);
    ID3D12GraphicsCommandList_RSSetViewports(list, 1, &vp);
    ID3D12GraphicsCommandList_RSSetScissorRects(list, 1, &scissor);
    ID3D12GraphicsCommandList_IASetPrimitiveTopology(list, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D12GraphicsCommandList_DrawInstanced(list, 3, 1, 0, 0);
    return S_OK;
}

#endif /* YUVBLIT_H */
