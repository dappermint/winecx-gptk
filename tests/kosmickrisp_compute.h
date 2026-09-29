/* One deterministic shader workload, not a general shader conformance test. */
#include "kosmickrisp_compute_spv.h"

static int compute_readback(PFN_vkGetInstanceProcAddr gipa, PFN_vkGetDeviceProcAddr gdpa,
    VkInstance instance, VkPhysicalDevice physical, VkDevice device, uint32_t family)
{
    RESOLVE(vkGetPhysicalDeviceMemoryProperties);
#define DEVICE_PROC(name) PFN_##name name = (PFN_##name)gdpa(device, #name); if (!name) return 1
    DEVICE_PROC(vkCreateBuffer); DEVICE_PROC(vkDestroyBuffer);
    DEVICE_PROC(vkGetBufferMemoryRequirements); DEVICE_PROC(vkBindBufferMemory);
    DEVICE_PROC(vkAllocateMemory); DEVICE_PROC(vkFreeMemory);
    DEVICE_PROC(vkMapMemory); DEVICE_PROC(vkUnmapMemory);
    DEVICE_PROC(vkCreateDescriptorSetLayout); DEVICE_PROC(vkDestroyDescriptorSetLayout);
    DEVICE_PROC(vkCreateDescriptorPool); DEVICE_PROC(vkDestroyDescriptorPool);
    DEVICE_PROC(vkAllocateDescriptorSets); DEVICE_PROC(vkUpdateDescriptorSets);
    DEVICE_PROC(vkCreateShaderModule); DEVICE_PROC(vkDestroyShaderModule);
    DEVICE_PROC(vkCreatePipelineLayout); DEVICE_PROC(vkDestroyPipelineLayout);
    DEVICE_PROC(vkCreateComputePipelines); DEVICE_PROC(vkDestroyPipeline);
    DEVICE_PROC(vkCreateCommandPool); DEVICE_PROC(vkDestroyCommandPool);
    DEVICE_PROC(vkAllocateCommandBuffers); DEVICE_PROC(vkBeginCommandBuffer);
    DEVICE_PROC(vkEndCommandBuffer); DEVICE_PROC(vkCmdPipelineBarrier);
    DEVICE_PROC(vkCmdBindPipeline); DEVICE_PROC(vkCmdBindDescriptorSets);
    DEVICE_PROC(vkCmdDispatch); DEVICE_PROC(vkGetDeviceQueue);
    DEVICE_PROC(vkQueueSubmit); DEVICE_PROC(vkQueueWaitIdle);
#undef DEVICE_PROC
    VkBufferCreateInfo buffer_info = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = 16, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT, .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
    VkBuffer buffer;
    CHECK(vkCreateBuffer(device, &buffer_info, NULL, &buffer));
    VkMemoryRequirements requirements;
    vkGetBufferMemoryRequirements(device, buffer, &requirements);
    VkPhysicalDeviceMemoryProperties memory_properties;
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);
    const VkMemoryPropertyFlags flags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    uint32_t type = 0;
    for (; type < memory_properties.memoryTypeCount; ++type)
        if ((requirements.memoryTypeBits & (1u << type)) &&
            (memory_properties.memoryTypes[type].propertyFlags & flags) == flags) break;
    if (type == memory_properties.memoryTypeCount) { fprintf(stderr, "no host-coherent storage memory\n"); return 1; }
    VkMemoryAllocateInfo allocation = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = requirements.size, .memoryTypeIndex = type };
    VkDeviceMemory memory;
    CHECK(vkAllocateMemory(device, &allocation, NULL, &memory));
    CHECK(vkBindBufferMemory(device, buffer, memory, 0));
    void *mapped;
    CHECK(vkMapMemory(device, memory, 0, 16, 0, &mapped));
    memset(mapped, 0, 16);
    VkDescriptorSetLayoutBinding binding = { .binding = 0,
        .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .descriptorCount = 1,
        .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT };
    VkDescriptorSetLayoutCreateInfo set_info = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 1, .pBindings = &binding };
    VkDescriptorSetLayout set_layout;
    CHECK(vkCreateDescriptorSetLayout(device, &set_info, NULL, &set_layout));
    VkDescriptorPoolSize pool_size = { VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1 };
    VkDescriptorPoolCreateInfo pool_info = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1, .poolSizeCount = 1, .pPoolSizes = &pool_size };
    VkDescriptorPool descriptor_pool;
    CHECK(vkCreateDescriptorPool(device, &pool_info, NULL, &descriptor_pool));
    VkDescriptorSetAllocateInfo set_allocate = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = descriptor_pool, .descriptorSetCount = 1, .pSetLayouts = &set_layout };
    VkDescriptorSet set;
    CHECK(vkAllocateDescriptorSets(device, &set_allocate, &set));
    VkDescriptorBufferInfo buffer_binding = { .buffer = buffer, .range = 16 };
    VkWriteDescriptorSet write = { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
        .dstSet = set, .dstBinding = 0, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &buffer_binding };
    vkUpdateDescriptorSets(device, 1, &write, 0, NULL);
    VkShaderModuleCreateInfo shader_info = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = sizeof(kosmickrisp_compute_spv), .pCode = kosmickrisp_compute_spv };
    VkShaderModule shader;
    CHECK(vkCreateShaderModule(device, &shader_info, NULL, &shader));
    VkPipelineLayoutCreateInfo layout_info = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1, .pSetLayouts = &set_layout };
    VkPipelineLayout layout;
    CHECK(vkCreatePipelineLayout(device, &layout_info, NULL, &layout));
    VkComputePipelineCreateInfo pipeline_info = { .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                   .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = shader, .pName = "main" },
        .layout = layout };
    VkPipeline pipeline;
    CHECK(vkCreateComputePipelines(device, VK_NULL_HANDLE, 1, &pipeline_info, NULL, &pipeline));
    VkCommandPoolCreateInfo command_pool_info = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .queueFamilyIndex = family };
    VkCommandPool command_pool;
    CHECK(vkCreateCommandPool(device, &command_pool_info, NULL, &command_pool));
    VkCommandBufferAllocateInfo command_allocate = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = command_pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
    VkCommandBuffer command;
    CHECK(vkAllocateCommandBuffers(device, &command_allocate, &command));
    VkCommandBufferBeginInfo begin = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    CHECK(vkBeginCommandBuffer(command, &begin));
    vkCmdBindPipeline(command, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
    vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &set, 0, NULL);
    vkCmdDispatch(command, 4, 1, 1);
    VkMemoryBarrier barrier = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
    vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
        0, 1, &barrier, 0, NULL, 0, NULL);
    CHECK(vkEndCommandBuffer(command));
    VkQueue queue;
    vkGetDeviceQueue(device, family, 0, &queue);
    VkSubmitInfo submit = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount = 1, .pCommandBuffers = &command };
    CHECK(vkQueueSubmit(queue, 1, &submit, VK_NULL_HANDLE));
    CHECK(vkQueueWaitIdle(queue));
    for (unsigned i = 0; i < 4; ++i) {
        uint32_t got = ((const uint32_t *)mapped)[i];
        if (got != (0xc0de0000u | i)) {
            fprintf(stderr, "shader readback [%u]: 0x%x\n", i, got);
            return 1;
        }
    }
    vkUnmapMemory(device, memory);
    vkDestroyCommandPool(device, command_pool, NULL);
    vkDestroyPipeline(device, pipeline, NULL);
    vkDestroyPipelineLayout(device, layout, NULL);
    vkDestroyShaderModule(device, shader, NULL);
    vkDestroyDescriptorPool(device, descriptor_pool, NULL);
    vkDestroyDescriptorSetLayout(device, set_layout, NULL);
    vkDestroyBuffer(device, buffer, NULL);
    vkFreeMemory(device, memory, NULL);
    puts("compute shader readback passed");
    return 0;
}
