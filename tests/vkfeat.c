/* which VkPhysicalDeviceFeatures does winevulkan/MoltenVK actually advertise?
 *
 * vkCreateDevice returns VK_ERROR_FEATURE_NOT_PRESENT when the app enables a
 * bool in VkPhysicalDeviceFeatures that the driver reports as FALSE. Nothing
 * else produces that code -- a missing extension is EXTENSION_NOT_PRESENT, a
 * missing layer is LAYER_NOT_PRESENT -- so when a title dies there the answer
 * is always one of the 55 bools below, and the only question is which.
 *
 * Declared against no headers on purpose: there is no vulkan SDK in this
 * toolchain, and VkPhysicalDeviceFeatures has been ABI-frozen since 1.0, so
 * the struct is safe to restate. Only the fields we read are named; the rest of
 * VkPhysicalDeviceProperties is padding sized well past its real 824 bytes.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -o vkfeat.exe vkfeat.c
 */
#include <windows.h>
#include <stdio.h>
#include <stdint.h>

typedef struct { uint32_t sType; const void *pNext; const char *pApplicationName;
                 uint32_t applicationVersion; const char *pEngineName;
                 uint32_t engineVersion; uint32_t apiVersion; } VkApplicationInfo;

typedef struct { uint32_t sType; const void *pNext; uint32_t flags;
                 const VkApplicationInfo *pApplicationInfo;
                 uint32_t enabledLayerCount; const char *const *ppEnabledLayerNames;
                 uint32_t enabledExtensionCount; const char *const *ppEnabledExtensionNames;
               } VkInstanceCreateInfo;

typedef struct { uint32_t apiVersion, driverVersion, vendorID, deviceID, deviceType;
                 char deviceName[256]; uint8_t pipelineCacheUUID[16];
                 uint8_t limits_and_sparse[2048]; } VkPhysicalDeviceProperties;

typedef struct { char extensionName[256]; uint32_t specVersion; } VkExtensionProperties;

/* the 55 bools, in ABI order */
#define FEATURES(F) \
    F(robustBufferAccess) F(fullDrawIndexUint32) F(imageCubeArray) F(independentBlend) \
    F(geometryShader) F(tessellationShader) F(sampleRateShading) F(dualSrcBlend) \
    F(logicOp) F(multiDrawIndirect) F(drawIndirectFirstInstance) F(depthClamp) \
    F(depthBiasClamp) F(fillModeNonSolid) F(depthBounds) F(wideLines) \
    F(largePoints) F(alphaToOne) F(multiViewport) F(samplerAnisotropy) \
    F(textureCompressionETC2) F(textureCompressionASTC_LDR) F(textureCompressionBC) \
    F(occlusionQueryPrecise) F(pipelineStatisticsQuery) F(vertexPipelineStoresAndAtomics) \
    F(fragmentStoresAndAtomics) F(shaderTessellationAndGeometryPointSize) \
    F(shaderImageGatherExtended) F(shaderStorageImageExtendedFormats) \
    F(shaderStorageImageMultisample) F(shaderStorageImageReadWithoutFormat) \
    F(shaderStorageImageWriteWithoutFormat) F(shaderUniformBufferArrayDynamicIndexing) \
    F(shaderSampledImageArrayDynamicIndexing) F(shaderStorageBufferArrayDynamicIndexing) \
    F(shaderStorageImageArrayDynamicIndexing) F(shaderClipDistance) F(shaderCullDistance) \
    F(shaderFloat64) F(shaderInt64) F(shaderInt16) F(shaderResourceResidency) \
    F(shaderResourceMinLod) F(sparseBinding) F(sparseResidencyBuffer) \
    F(sparseResidencyImage2D) F(sparseResidencyImage3D) F(sparseResidency2Samples) \
    F(sparseResidency4Samples) F(sparseResidency8Samples) F(sparseResidency16Samples) \
    F(sparseResidencyAliased) F(variableMultisampleRate) F(inheritedQueries)

#define AS_FIELD(n) uint32_t n;
typedef struct { FEATURES(AS_FIELD) } VkPhysicalDeviceFeatures;

#define AS_NAME(n) #n,
static const char *FEATURE_NAMES[] = { FEATURES(AS_NAME) };
#define FEATURE_COUNT (sizeof(FEATURE_NAMES) / sizeof(FEATURE_NAMES[0]))

typedef void *VkInstance, *VkPhysicalDevice;
typedef int32_t VkResult;

static int32_t (*pCreateInstance)(const VkInstanceCreateInfo *, const void *, VkInstance *);
static int32_t (*pEnumeratePhysicalDevices)(VkInstance, uint32_t *, VkPhysicalDevice *);
static void    (*pGetPhysicalDeviceProperties)(VkPhysicalDevice, VkPhysicalDeviceProperties *);
static void    (*pGetPhysicalDeviceFeatures)(VkPhysicalDevice, VkPhysicalDeviceFeatures *);
static int32_t (*pEnumerateDeviceExtensionProperties)(VkPhysicalDevice, const char *,
                                                      uint32_t *, VkExtensionProperties *);
static int32_t (*pEnumerateInstanceVersion)(uint32_t *);

int main(void)
{
    HMODULE vk = LoadLibraryA("vulkan-1.dll");
    if (!vk) { printf("no vulkan-1.dll (%lu)\n", GetLastError()); return 1; }

#define LOAD(n) do { p##n = (void *)GetProcAddress(vk, "vk" #n); \
    if (!p##n) { printf("missing vk" #n "\n"); return 1; } } while (0)
    LOAD(CreateInstance);
    LOAD(EnumeratePhysicalDevices);
    LOAD(GetPhysicalDeviceProperties);
    LOAD(GetPhysicalDeviceFeatures);
    LOAD(EnumerateDeviceExtensionProperties);
#undef LOAD
    /* not present pre-1.1, so this one is allowed to be missing */
    pEnumerateInstanceVersion = (void *)GetProcAddress(vk, "vkEnumerateInstanceVersion");

    /* The version a device reports can be clamped to what the *instance* asked
     * for, so asking low and then reading the answer measures the question, not
     * the driver. Ask for the highest the stack admits to supporting. */
    uint32_t inst_ver = (1 << 22);
    if (pEnumerateInstanceVersion && !pEnumerateInstanceVersion(&inst_ver))
        printf("[info] vkEnumerateInstanceVersion: %u.%u.%u\n",
               inst_ver >> 22, (inst_ver >> 12) & 0x3ff, inst_ver & 0xfff);
    else
        printf("[info] no vkEnumerateInstanceVersion, stack is Vulkan 1.0\n");

    VkApplicationInfo app = { 0 };
    app.sType = 0;
    app.pApplicationName = "vkfeat";
    app.pEngineName = "vkfeat";
    app.apiVersion = inst_ver;  /* ask for everything the instance admits to */

    VkInstanceCreateInfo ici = { 0 };
    ici.sType = 1;
    ici.pApplicationInfo = &app;

    VkInstance inst = NULL;
    VkResult r = pCreateInstance(&ici, NULL, &inst);
    if (r) { printf("vkCreateInstance failed: %d\n", r); return 2; }

    uint32_t count = 0;
    pEnumeratePhysicalDevices(inst, &count, NULL);
    if (!count) { printf("no physical devices\n"); return 3; }
    VkPhysicalDevice devs[8];
    if (count > 8) count = 8;
    pEnumeratePhysicalDevices(inst, &count, devs);

    for (uint32_t d = 0; d < count; d++) {
        VkPhysicalDeviceProperties props = { 0 };
        pGetPhysicalDeviceProperties(devs[d], &props);
        printf("=== device %u: %s (api %u.%u.%u, vendor 0x%04x)\n", d, props.deviceName,
               props.apiVersion >> 22, (props.apiVersion >> 12) & 0x3ff,
               props.apiVersion & 0xfff, props.vendorID);

        VkPhysicalDeviceFeatures f = { 0 };
        pGetPhysicalDeviceFeatures(devs[d], &f);
        const uint32_t *bits = (const uint32_t *)&f;

        printf("--- NOT supported (a title enabling any of these gets VK_ERROR_FEATURE_NOT_PRESENT)\n");
        unsigned missing = 0;
        for (unsigned i = 0; i < FEATURE_COUNT; i++)
            if (!bits[i]) { printf("  [ ] %s\n", FEATURE_NAMES[i]); missing++; }
        printf("--- supported\n");
        for (unsigned i = 0; i < FEATURE_COUNT; i++)
            if (bits[i]) printf("  [x] %s\n", FEATURE_NAMES[i]);
        printf("--- %u of %u features missing\n", missing, (unsigned)FEATURE_COUNT);

        uint32_t ec = 0;
        pEnumerateDeviceExtensionProperties(devs[d], NULL, &ec, NULL);
        printf("--- %u device extensions\n", ec);
        if (ec) {
            VkExtensionProperties *ext = malloc(ec * sizeof(*ext));
            pEnumerateDeviceExtensionProperties(devs[d], NULL, &ec, ext);
            for (uint32_t i = 0; i < ec; i++) printf("  %s\n", ext[i].extensionName);
            free(ext);
        }
    }
    return 0;
}
