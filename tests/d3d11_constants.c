#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(call) do { HRESULT check_hr = (call); if (FAILED(check_hr)) { fprintf(stderr, "%s: 0x%08lx\n", #call, (unsigned long)check_hr); return 1; } } while (0)

static ID3DBlob *compile(const char *source, const char *target)
{
    ID3DBlob *code = NULL, *errors = NULL;
    HRESULT hr = D3DCompile(source, strlen(source), "constant-array-probe", NULL, NULL,
                           "main", target, D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &code, &errors);
    if (FAILED(hr)) {
        if (errors) fprintf(stderr, "%.*s\n", (int)ID3D10Blob_GetBufferSize(errors),
                            (char *)ID3D10Blob_GetBufferPointer(errors));
        fprintf(stderr, "D3DCompile %s: 0x%08lx\n", target, (unsigned long)hr);
    }
    if (errors) ID3D10Blob_Release(errors);
    return code;
}

int main(void)
{
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    D3D_FEATURE_LEVEL level;
    CHECK(D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, NULL, 0,
                           D3D11_SDK_VERSION, &device, &level, &context));
    const char *vs_source = "float4 main(uint id:SV_VertexID):SV_Position { float2 p=float2((id<<1)&2,id&2); return float4(p*float2(2,-2)+float2(-1,1),0,1); }";
    ID3DBlob *vs_code = compile(vs_source, "vs_5_0");
    if (!vs_code) return 1;
    ID3D11VertexShader *vs = NULL;
    CHECK(ID3D11Device_CreateVertexShader(device, ID3D10Blob_GetBufferPointer(vs_code),
                                         ID3D10Blob_GetBufferSize(vs_code), NULL, &vs));
    ID3D10Blob_Release(vs_code);
    D3D11_TEXTURE2D_DESC desc = {0};
    desc.Width = desc.Height = 16;
    desc.MipLevels = desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R32G32B32A32_UINT;
    desc.SampleDesc.Count = 1;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D *texture = NULL, *readback = NULL;
    CHECK(ID3D11Device_CreateTexture2D(device, &desc, NULL, &texture));
    desc.BindFlags = 0;
    desc.Usage = D3D11_USAGE_STAGING;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    CHECK(ID3D11Device_CreateTexture2D(device, &desc, NULL, &readback));
    ID3D11RenderTargetView *rtv = NULL;
    CHECK(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)texture, NULL, &rtv));
    D3D11_BUFFER_DESC cb_desc = {0};
    cb_desc.ByteWidth = 16;
    cb_desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    ID3D11Buffer *cb = NULL;
    CHECK(ID3D11Device_CreateBuffer(device, &cb_desc, NULL, &cb));
    D3D11_VIEWPORT viewport = {0, 0, 16, 16, 0, 1};
    ID3D11DeviceContext_RSSetViewports(context, 1, &viewport);
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &rtv, NULL);
    ID3D11DeviceContext_IASetPrimitiveTopology(context, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(context, vs, NULL, 0);
    ID3D11DeviceContext_PSSetConstantBuffers(context, 0, 1, &cb);
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    unsigned checked = 0;
    for (unsigned size = 64; size <= 1024; size *= 4) {
        size_t capacity = size * 90 + 1024;
        char *source = malloc(capacity);
        if (!source) return 1;
        int length = snprintf(source, capacity, "cbuffer Selector:register(b0) { uint selector; }; static const uint4 values[%u]={", size);
        for (unsigned i = 0; i < size; i++)
            length += snprintf(source + length, capacity - length, "%suint4(%u,%u,%u,%u)",
                               i ? "," : "", i, i * 3 + 1, i ^ 0x12345u, (i * i + 17) & 0xffffu);
        snprintf(source + length, capacity - length, "}; uint4 main(float4 position:SV_Position):SV_Target { uint index=(selector+uint(position.x)+7*uint(position.y))&%u; return values[index]; }", size - 1);
        ID3DBlob *ps_code = compile(source, "ps_5_0");
        free(source);
        if (!ps_code) return 1;
        ID3D11PixelShader *ps = NULL;
        LARGE_INTEGER start, end;
        QueryPerformanceCounter(&start);
        CHECK(ID3D11Device_CreatePixelShader(device, ID3D10Blob_GetBufferPointer(ps_code),
                                            ID3D10Blob_GetBufferSize(ps_code), NULL, &ps));
        ID3D10Blob_Release(ps_code);
        ID3D11DeviceContext_PSSetShader(context, ps, NULL, 0);
        for (unsigned selector = 0; selector < size; selector += 17) {
            uint32_t data[4] = {selector, 0, 0, 0};
            ID3D11DeviceContext_UpdateSubresource(context, (ID3D11Resource *)cb, 0, NULL, data, 0, 0);
            ID3D11DeviceContext_Draw(context, 3, 0);
            ID3D11DeviceContext_CopyResource(context, (ID3D11Resource *)readback, (ID3D11Resource *)texture);
            D3D11_MAPPED_SUBRESOURCE mapped;
            CHECK(ID3D11DeviceContext_Map(context, (ID3D11Resource *)readback, 0, D3D11_MAP_READ, 0, &mapped));
            for (unsigned y = 0; y < 16; y++) {
                const uint32_t *row = (const uint32_t *)((const char *)mapped.pData + y * mapped.RowPitch);
                for (unsigned x = 0; x < 16; x++) {
                    unsigned index = (selector + x + 7 * y) & (size - 1);
                    uint32_t expected[4] = {index, index * 3 + 1, index ^ 0x12345u, (index * index + 17) & 0xffffu};
                    if (memcmp(row + x * 4, expected, sizeof(expected))) {
                        fprintf(stderr, "readback mismatch size=%u selector=%u pixel=%u,%u got=%u,%u,%u,%u\n",
                                size, selector, x, y, row[x*4], row[x*4+1], row[x*4+2], row[x*4+3]);
                        return 1;
                    }
                    checked++;
                }
            }
            ID3D11DeviceContext_Unmap(context, (ID3D11Resource *)readback, 0);
        }
        QueryPerformanceCounter(&end);
        printf("array=%u elapsed_ms=%.3f\n", size, (double)(end.QuadPart - start.QuadPart) * 1000 / frequency.QuadPart);
        ID3D11PixelShader_Release(ps);
    }
    ID3D11DeviceContext_ClearState(context);
    ID3D11DeviceContext_Flush(context);
    ID3D11Buffer_Release(cb);
    ID3D11RenderTargetView_Release(rtv);
    ID3D11Texture2D_Release(readback);
    ID3D11Texture2D_Release(texture);
    ID3D11VertexShader_Release(vs);
    ID3D11DeviceContext_Release(context);
    ID3D11Device_Release(device);
    printf("RESULT: constant-array readback passed (%u pixels)\n", checked);
    return 0;
}
