/* Loader/ICD smoke test shared by the x86_64 host and Wine PE builds.
 * Uses real Vulkan headers so Features2/pNext queries retain the correct ABI.
 * This proves enumeration and basic device creation, plus one clear/present
 * with --present on Windows. It does not establish DXVK compatibility.
 * Missing optional features are reported only.
 */
#define VK_NO_PROTOTYPES
#ifdef _WIN32
#define VK_USE_PLATFORM_WIN32_KHR
#endif
#include <vulkan/vulkan.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#endif

#define RESOLVE(name) PFN_##name name = (PFN_##name)gipa(instance, #name); \
    if (!name) { fprintf(stderr, "missing %s\n", #name); return 1; }
#define CHECK(call) do { VkResult result = (call); if (result != VK_SUCCESS) { \
    fprintf(stderr, "%s: %d\n", #call, result); return 1; } } while (0)

#ifdef _WIN32
#include "kosmickrisp_present.h"
#include "kosmickrisp_compute.h"
#endif

int main(int argc, char **argv)
{
    PFN_vkGetInstanceProcAddr gipa;
    int present = 0;
#ifdef _WIN32
    present = argc == 2 && !strcmp(argv[1], "--present");
    HMODULE module = LoadLibraryA("vulkan-1.dll");
    if (!module) { fprintf(stderr, "LoadLibrary: %lu\n", GetLastError()); return 1; }
    /* Through a generic function pointer: gcc's -Wcast-function-type rejects
     * a direct FARPROC cast. */
    gipa = (PFN_vkGetInstanceProcAddr)(void (*)(void))GetProcAddress(module, "vkGetInstanceProcAddr");
#else
    if (argc != 2) { fprintf(stderr, "usage: %s LOADER_DYLIB\n", argv[0]); return 1; }
    void *module = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!module) { fprintf(stderr, "%s\n", dlerror()); return 1; }
    gipa = (PFN_vkGetInstanceProcAddr)dlsym(module, "vkGetInstanceProcAddr");
#endif
    if (!gipa) return 1;
    VkInstance instance = VK_NULL_HANDLE;
    RESOLVE(vkCreateInstance);
    VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "winecx KosmicKrisp probe", .apiVersion = VK_API_VERSION_1_3 };
    VkInstanceCreateInfo create = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &app };
    const char *instance_extensions[] = { "VK_KHR_surface", "VK_KHR_win32_surface" };
    if (present) {
        create.enabledExtensionCount = 2;
        create.ppEnabledExtensionNames = instance_extensions;
    }
    CHECK(vkCreateInstance(&create, NULL, &instance));
    RESOLVE(vkDestroyInstance);
    RESOLVE(vkEnumeratePhysicalDevices);
    RESOLVE(vkGetPhysicalDeviceProperties2);
    RESOLVE(vkGetPhysicalDeviceFeatures2);
    RESOLVE(vkEnumerateDeviceExtensionProperties);
    RESOLVE(vkGetPhysicalDeviceQueueFamilyProperties);
    RESOLVE(vkCreateDevice);
    RESOLVE(vkGetDeviceProcAddr);

    uint32_t count = 0;
    CHECK(vkEnumeratePhysicalDevices(instance, &count, NULL));
    if (!count) { fprintf(stderr, "no Vulkan devices\n"); return 1; }
    VkPhysicalDevice *devices = calloc(count, sizeof(*devices));
    if (!devices) return 1;
    CHECK(vkEnumeratePhysicalDevices(instance, &count, devices));
    unsigned found = 0;
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceDriverProperties driver = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES };
        VkPhysicalDeviceProperties2 props = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &driver };
        vkGetPhysicalDeviceProperties2(devices[i], &props);
        printf("device=%s driver=%s id=%u info=%s\n", props.properties.deviceName,
            driver.driverName, driver.driverID, driver.driverInfo);
        if (driver.driverID != VK_DRIVER_ID_MESA_KOSMICKRISP) continue;
        ++found;
        VkPhysicalDeviceVulkan13Features f13 = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES };
        VkPhysicalDeviceVulkan12Features f12 = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES, .pNext = &f13 };
        VkPhysicalDeviceFeatures2 features = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &f12 };
        vkGetPhysicalDeviceFeatures2(devices[i], &features);
        printf("api=%u.%u.%u maxPushConstantsSize=%u\n",
            VK_API_VERSION_MAJOR(props.properties.apiVersion),
            VK_API_VERSION_MINOR(props.properties.apiVersion),
            VK_API_VERSION_PATCH(props.properties.apiVersion), props.properties.limits.maxPushConstantsSize);
        printf("geometryShader=%u tessellationShader=%u shaderInt64=%u fillModeNonSolid=%u\n",
            features.features.geometryShader, features.features.tessellationShader,
            features.features.shaderInt64, features.features.fillModeNonSolid);
        printf("descriptorIndexing=%u scalarBlockLayout=%u shaderInt8=%u synchronization2=%u\n",
            f12.descriptorIndexing, f12.scalarBlockLayout, f12.shaderInt8, f13.synchronization2);

        uint32_t extension_count = 0;
        CHECK(vkEnumerateDeviceExtensionProperties(devices[i], NULL, &extension_count, NULL));
        VkExtensionProperties *extensions = calloc(extension_count, sizeof(*extensions));
        if (!extensions) return 1;
        CHECK(vkEnumerateDeviceExtensionProperties(devices[i], NULL, &extension_count, extensions));
        unsigned xfb = 0;
        for (uint32_t j = 0; j < extension_count; ++j) {
            printf("extension=%s\n", extensions[j].extensionName);
            if (!strcmp(extensions[j].extensionName, VK_EXT_TRANSFORM_FEEDBACK_EXTENSION_NAME)) xfb = 1;
        }
        free(extensions);
        if (xfb) {
            VkPhysicalDeviceTransformFeedbackFeaturesEXT transform = {
                .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_TRANSFORM_FEEDBACK_FEATURES_EXT };
            features.pNext = &transform;
            vkGetPhysicalDeviceFeatures2(devices[i], &features);
            printf("transformFeedback=%u geometryStreams=%u\n", transform.transformFeedback, transform.geometryStreams);
        } else puts("VK_EXT_transform_feedback absent; upstream DXVK requirements unmet");

        uint32_t queue_count = 0;
        vkGetPhysicalDeviceQueueFamilyProperties(devices[i], &queue_count, NULL);
        VkQueueFamilyProperties *queues = calloc(queue_count, sizeof(*queues));
        if (!queues) return 1;
        vkGetPhysicalDeviceQueueFamilyProperties(devices[i], &queue_count, queues);
        uint32_t family = 0;
        VkQueueFlags required_flags = VK_QUEUE_GRAPHICS_BIT | (present ? VK_QUEUE_COMPUTE_BIT : 0);
        while (family < queue_count && (queues[family].queueFlags & required_flags) != required_flags) ++family;
        free(queues);
        if (family == queue_count) { fprintf(stderr, "no graphics queue\n"); return 1; }
        float priority = 1.0f;
        VkDeviceQueueCreateInfo queue = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
            .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority };
        VkDeviceCreateInfo device_info = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
            .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue };
        const char *swapchain_extension = VK_KHR_SWAPCHAIN_EXTENSION_NAME;
        if (present) {
            device_info.enabledExtensionCount = 1;
            device_info.ppEnabledExtensionNames = &swapchain_extension;
        }
        VkDevice device;
        CHECK(vkCreateDevice(devices[i], &device_info, NULL, &device));
        PFN_vkDestroyDevice destroy = (PFN_vkDestroyDevice)vkGetDeviceProcAddr(device, "vkDestroyDevice");
        if (!destroy) return 1;
#ifdef _WIN32
        if (present && compute_readback(gipa, vkGetDeviceProcAddr, instance, devices[i], device, family)) return 1;
        if (present && present_frame(gipa, vkGetDeviceProcAddr, instance, devices[i], device, family)) return 1;
#endif
        destroy(device, NULL);
        puts("KosmicKrisp device creation passed (no DXVK compatibility claim)");
    }
    free(devices);
    vkDestroyInstance(instance, NULL);
    if (!found) { fprintf(stderr, "KosmicKrisp not selected; refusing a fallback result\n"); return 1; }
    return 0;
}
