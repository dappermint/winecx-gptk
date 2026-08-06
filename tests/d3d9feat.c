/* does d3d9 come up, and on which backend?
 *
 * the answer that matters is the adapter string. DXVK reports the real GPU
 * through MoltenVK ("Apple M..."), wined3d reports whatever apple's OpenGL
 * says and caps out at feature level 9_3-era capabilities. shipping a d3d9.dll
 * that fails to load is silent: wine falls back to its builtin and everything
 * still runs, just slowly and wrongly.
 */
#include <windows.h>
#include <d3d9.h>
#include <stdio.h>

int main(void)
{
    IDirect3D9 *d3d;
    IDirect3DDevice9 *dev = NULL;
    D3DADAPTER_IDENTIFIER9 id;
    D3DCAPS9 caps;
    D3DPRESENT_PARAMETERS pp;
    WNDCLASSA wc;
    HWND hwnd;
    HRESULT hr;

    d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!d3d)
    {
        printf("FAIL: Direct3DCreate9 returned NULL\n");
        return 1;
    }
    printf("[ ok ] Direct3DCreate9\n");

    memset(&id, 0, sizeof(id));
    hr = IDirect3D9_GetAdapterIdentifier(d3d, D3DADAPTER_DEFAULT, 0, &id);
    if (FAILED(hr))
    {
        printf("FAIL: GetAdapterIdentifier %08lx\n", (unsigned long)hr);
        return 1;
    }
    printf("[info] adapter: %s | driver %s | vendor 0x%04x device 0x%04x\n",
           id.Description, id.Driver, id.VendorId, id.DeviceId);

    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "d3d9feat";
    RegisterClassA(&wc);
    hwnd = CreateWindowExA(0, "d3d9feat", "d3d9feat", WS_OVERLAPPEDWINDOW,
                           0, 0, 640, 480, NULL, NULL, wc.hInstance, NULL);
    if (!hwnd)
    {
        printf("FAIL: CreateWindowEx %lu\n", (unsigned long)GetLastError());
        return 1;
    }

    memset(&pp, 0, sizeof(pp));
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferFormat = D3DFMT_UNKNOWN;
    pp.hDeviceWindow = hwnd;

    hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, hwnd,
                                 D3DCREATE_HARDWARE_VERTEXPROCESSING, &pp, &dev);
    if (FAILED(hr))
    {
        printf("FAIL: CreateDevice %08lx\n", (unsigned long)hr);
        return 1;
    }
    printf("[ ok ] CreateDevice D3DDEVTYPE_HAL\n");

    memset(&caps, 0, sizeof(caps));
    hr = IDirect3DDevice9_GetDeviceCaps(dev, &caps);
    if (FAILED(hr))
    {
        printf("FAIL: GetDeviceCaps %08lx\n", (unsigned long)hr);
        return 1;
    }
    printf("[info] vs %u.%u ps %u.%u, max texture %ux%u, %u simultaneous rts\n",
           (caps.VertexShaderVersion >> 8) & 0xff, caps.VertexShaderVersion & 0xff,
           (caps.PixelShaderVersion >> 8) & 0xff, caps.PixelShaderVersion & 0xff,
           caps.MaxTextureWidth, caps.MaxTextureHeight,
           caps.NumSimultaneousRTs);

    /* shader model 3.0 is the discriminator: wined3d over apple's GL cannot
       reach it, DXVK reports 3.0 because MoltenVK backs it */
    if (caps.PixelShaderVersion < D3DPS_VERSION(3, 0))
        printf("[warn] pixel shader %u.%u is below 3.0; this is not DXVK\n",
               (caps.PixelShaderVersion >> 8) & 0xff, caps.PixelShaderVersion & 0xff);

    IDirect3DDevice9_Release(dev);
    IDirect3D9_Release(d3d);
    DestroyWindow(hwnd);
    printf("RESULT: d3d9 device up\n");
    return 0;
}
