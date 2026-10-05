/* Optional Windows WSI test: clear and present one image, without DXVK/shaders. */
static int present_frame(PFN_vkGetInstanceProcAddr gipa, PFN_vkGetDeviceProcAddr gdpa,
    VkInstance instance, VkPhysicalDevice physical, VkDevice device, uint32_t family)
{
    RESOLVE(vkCreateWin32SurfaceKHR);
    RESOLVE(vkDestroySurfaceKHR);
    RESOLVE(vkGetPhysicalDeviceSurfaceSupportKHR);
    RESOLVE(vkGetPhysicalDeviceSurfaceCapabilitiesKHR);
    RESOLVE(vkGetPhysicalDeviceSurfaceFormatsKHR);
#define DEVICE_PROC(name) PFN_##name name = (PFN_##name)gdpa(device, #name); if (!name) return 1
    DEVICE_PROC(vkCreateSwapchainKHR); DEVICE_PROC(vkDestroySwapchainKHR);
    DEVICE_PROC(vkGetSwapchainImagesKHR); DEVICE_PROC(vkAcquireNextImageKHR);
    DEVICE_PROC(vkGetDeviceQueue); DEVICE_PROC(vkQueuePresentKHR);
    DEVICE_PROC(vkQueueSubmit); DEVICE_PROC(vkQueueWaitIdle);
    DEVICE_PROC(vkCreateSemaphore); DEVICE_PROC(vkDestroySemaphore);
    DEVICE_PROC(vkCreateCommandPool); DEVICE_PROC(vkDestroyCommandPool);
    DEVICE_PROC(vkAllocateCommandBuffers); DEVICE_PROC(vkBeginCommandBuffer);
    DEVICE_PROC(vkEndCommandBuffer); DEVICE_PROC(vkCmdPipelineBarrier);
    DEVICE_PROC(vkCmdClearColorImage);
#undef DEVICE_PROC
    HINSTANCE application = GetModuleHandleA(NULL);
    WNDCLASSA wc = {0};
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = application;
    wc.lpszClassName = "KosmicKrispPresentationProbe";
    if (!RegisterClassA(&wc)) return 1;
    HWND window = CreateWindowA(wc.lpszClassName, "KosmicKrisp presentation test",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 640, 480,
        NULL, NULL, application, NULL);
    if (!window) return 1;
    VkWin32SurfaceCreateInfoKHR surface_info = { .sType = VK_STRUCTURE_TYPE_WIN32_SURFACE_CREATE_INFO_KHR,
        .hinstance = application, .hwnd = window };
    VkSurfaceKHR surface;
    CHECK(vkCreateWin32SurfaceKHR(instance, &surface_info, NULL, &surface));
    VkBool32 supported = VK_FALSE;
    CHECK(vkGetPhysicalDeviceSurfaceSupportKHR(physical, family, surface, &supported));
    if (!supported) { fprintf(stderr, "graphics queue cannot present\n"); return 1; }
    /* Recreate the swapchain on the same window/surface after each transition. */
    const char *modes[] = { "windowed", "resized", "borderless-fullscreen", "restored" };
    for (unsigned mode = 0; mode < 4; ++mode) {
        if (mode == 1 || mode == 3) {
            SetWindowLongPtrA(window, GWL_STYLE, WS_OVERLAPPEDWINDOW | WS_VISIBLE);
            if (!SetWindowPos(window, NULL, 80, 80, mode == 1 ? 800 : 640,
                              mode == 1 ? 600 : 480, SWP_FRAMECHANGED | SWP_NOZORDER)) return 1;
        } else if (mode == 2) {
            MONITORINFO monitor = { .cbSize = sizeof(monitor) };
            if (!GetMonitorInfoA(MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST), &monitor)) return 1;
            SetWindowLongPtrA(window, GWL_STYLE, WS_POPUP | WS_VISIBLE);
            if (!SetWindowPos(window, NULL, monitor.rcMonitor.left, monitor.rcMonitor.top,
                              monitor.rcMonitor.right - monitor.rcMonitor.left,
                              monitor.rcMonitor.bottom - monitor.rcMonitor.top,
                              SWP_FRAMECHANGED | SWP_NOZORDER)) return 1;
        }
        MSG message;
        while (PeekMessageA(&message, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageA(&message);
        }
        VkSurfaceCapabilitiesKHR caps;
        CHECK(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &caps));
        if (!(caps.supportedUsageFlags & VK_IMAGE_USAGE_TRANSFER_DST_BIT)) return 1;
        uint32_t count = 0;
        CHECK(vkGetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &count, NULL));
        if (!count) return 1;
        VkSurfaceFormatKHR *formats = calloc(count, sizeof(*formats));
        if (!formats) return 1;
        CHECK(vkGetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &count, formats));
        VkSurfaceFormatKHR format = formats[0];
        free(formats);
        if (format.format == VK_FORMAT_UNDEFINED) format.format = VK_FORMAT_B8G8R8A8_UNORM;
        VkExtent2D extent = caps.currentExtent;
        if (extent.width == UINT32_MAX) extent = caps.minImageExtent;
        if (!extent.width || !extent.height) { fprintf(stderr, "zero surface extent\n"); return 1; }
        count = caps.minImageCount + 1;
        if (caps.maxImageCount && count > caps.maxImageCount) count = caps.maxImageCount;
        VkCompositeAlphaFlagBitsKHR alpha = (VkCompositeAlphaFlagBitsKHR)
            (caps.supportedCompositeAlpha & (~caps.supportedCompositeAlpha + 1));
        VkSwapchainCreateInfoKHR swap_info = { .sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR,
            .surface = surface, .minImageCount = count, .imageFormat = format.format,
            .imageColorSpace = format.colorSpace, .imageExtent = extent, .imageArrayLayers = 1,
            .imageUsage = VK_IMAGE_USAGE_TRANSFER_DST_BIT, .imageSharingMode = VK_SHARING_MODE_EXCLUSIVE,
            .preTransform = caps.currentTransform, .compositeAlpha = alpha,
            .presentMode = VK_PRESENT_MODE_FIFO_KHR, .clipped = VK_TRUE };
        VkSwapchainKHR swapchain;
        CHECK(vkCreateSwapchainKHR(device, &swap_info, NULL, &swapchain));
        CHECK(vkGetSwapchainImagesKHR(device, swapchain, &count, NULL));
        VkImage *images = calloc(count, sizeof(*images));
        if (!images) return 1;
        CHECK(vkGetSwapchainImagesKHR(device, swapchain, &count, images));
        VkCommandPoolCreateInfo pool_info = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
            .queueFamilyIndex = family };
        VkCommandPool pool;
        CHECK(vkCreateCommandPool(device, &pool_info, NULL, &pool));
        VkCommandBufferAllocateInfo allocate = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
            .commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
        VkCommandBuffer command;
        CHECK(vkAllocateCommandBuffers(device, &allocate, &command));
        VkSemaphoreCreateInfo semaphore_info = { .sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO };
        VkSemaphore acquired, rendered;
        CHECK(vkCreateSemaphore(device, &semaphore_info, NULL, &acquired));
        CHECK(vkCreateSemaphore(device, &semaphore_info, NULL, &rendered));
        uint32_t index;
        CHECK(vkAcquireNextImageKHR(device, swapchain, UINT64_MAX, acquired, VK_NULL_HANDLE, &index));
        VkCommandBufferBeginInfo begin = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
        CHECK(vkBeginCommandBuffer(command, &begin));
        VkImageMemoryBarrier barrier = { .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
            .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED,
            .newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = images[index],
            .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
            0, 0, NULL, 0, NULL, 1, &barrier);
        VkClearColorValue color = { .float32 = {0.1f, 0.2f, 0.7f, 1.0f} };
        vkCmdClearColorImage(command, images[index], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            &color, 1, &barrier.subresourceRange);
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = 0;
        barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
            0, 0, NULL, 0, NULL, 1, &barrier);
        CHECK(vkEndCommandBuffer(command));
        VkQueue queue;
        vkGetDeviceQueue(device, family, 0, &queue);
        VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_TRANSFER_BIT;
        VkSubmitInfo submit = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .waitSemaphoreCount = 1,
            .pWaitSemaphores = &acquired, .pWaitDstStageMask = &wait_stage,
            .commandBufferCount = 1, .pCommandBuffers = &command,
            .signalSemaphoreCount = 1, .pSignalSemaphores = &rendered };
        CHECK(vkQueueSubmit(queue, 1, &submit, VK_NULL_HANDLE));
        VkPresentInfoKHR present = { .sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR,
            .waitSemaphoreCount = 1, .pWaitSemaphores = &rendered,
            .swapchainCount = 1, .pSwapchains = &swapchain, .pImageIndices = &index };
        CHECK(vkQueuePresentKHR(queue, &present));
        CHECK(vkQueueWaitIdle(queue));
        vkDestroyCommandPool(device, pool, NULL);
        vkDestroySwapchainKHR(device, swapchain, NULL);
        vkDestroySemaphore(device, acquired, NULL);
        vkDestroySemaphore(device, rendered, NULL);
        free(images);
        printf("presentation-mode=%s passed\n", modes[mode]);
    }
    vkDestroySurfaceKHR(instance, surface, NULL);
    DestroyWindow(window);
    puts("Win32 surface, swapchain, clear, and present passed");
    return 0;
}
