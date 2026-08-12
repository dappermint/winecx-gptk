/* vulkan-1.dll shim for running DOOM (2016) on wine + MoltenVK.
 *
 * Three separate problems live here, in the order they were hit:
 *
 * 1. vkCreateDevice returned VK_ERROR_FEATURE_NOT_PRESENT and neither Vulkan
 *    nor the game will say which of the 55 VkPhysicalDeviceFeatures bools was
 *    at fault. Reporting them from in between is the only way to name it, and
 *    masking the unsupported ones is the only way to get a device at all.
 *
 * 2. The game then died in SwitchToFiber+0x44 on 0x8ff. GetCurrentFiber() is a
 *    macro, `__readgsqword(0x20)`, which compiles inline and never enters wine.
 *    On macOS gs belongs to libpthread and only gs:0x30 is made to hold the TEB,
 *    so the read returns a _pthread_priority_t instead of NtTib.FiberData. The
 *    single instruction is rewritten to call a thunk that reads the real field.
 *
 * 3. With both of those fixed the game runs, but renders artifacts, which is
 *    the bill for step 1. The shader and pipeline counters measure whether the
 *    masked features are genuinely used or merely declared.
 *
 * Everything else forwards to winevulkan (see vkshim.def), so nothing but these
 * paths carries any cost.
 *
 * VKSHIM_LOG=<windows path>  where to write (default vkshim.log beside the exe)
 * VKSHIM_MASK=0              leave unsupported bits set so vkCreateDevice fails
 *                            as it would unaided; masking is on by default.
 *
 * Masking is a wager, not a fix: the title believes it got a feature it did
 * not, and anything depending on the driver honouring it renders wrong rather
 * than erroring. Worth it only for features Metal cannot express at all, where
 * the alternative is not running.
 *
 * build: x86_64-w64-mingw32-gcc -O2 -shared -o vulkan-1.dll vkshim.c vkshim.def
 */
#include <windows.h>
#include <stdio.h>
#include <stdint.h>

#define VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2 1000059000

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

typedef struct { uint32_t sType; void *pNext; VkPhysicalDeviceFeatures features; }
    VkPhysicalDeviceFeatures2;

typedef struct {
    uint32_t sType; const void *pNext; uint32_t flags;
    uint32_t queueCreateInfoCount; const void *pQueueCreateInfos;
    uint32_t enabledLayerCount; const char *const *ppEnabledLayerNames;
    uint32_t enabledExtensionCount; const char *const *ppEnabledExtensionNames;
    const VkPhysicalDeviceFeatures *pEnabledFeatures;
} VkDeviceCreateInfo;

typedef struct { uint32_t sType; const void *pNext; uint32_t flags;
                 size_t codeSize; const uint32_t *pCode; } VkShaderModuleCreateInfo;

typedef void *VkPhysicalDevice, *VkDevice, *VkInstance;
typedef int32_t VkResult;
typedef void (*PFN_vkVoidFunction)(void);

static int32_t (*real_vkCreateDevice)(VkPhysicalDevice, const VkDeviceCreateInfo *,
                                      const void *, VkDevice *);
static PFN_vkVoidFunction (*real_vkGetInstanceProcAddr)(VkInstance, const char *);
static PFN_vkVoidFunction (*real_vkGetDeviceProcAddr)(VkDevice, const char *);
static void (*real_vkGetPhysicalDeviceFeatures)(VkPhysicalDevice, VkPhysicalDeviceFeatures *);
static int32_t (*real_vkCreateShaderModule)(VkDevice, const VkShaderModuleCreateInfo *,
                                            const void *, void *);
static int32_t (*real_vkCreateGraphicsPipelines)(VkDevice, void *, uint32_t,
                                                 const void *, const void *, void *);

static FILE *logf_;
static int mask_mode;

/* ------------------------------------------------------------------ where */

/* idTech's crash handler catches faults first and cannot symbolize anything
 * under wine ("idStackTracer::GetSource: Failed"), so its report is one
 * truncated address and a guessed module. Resolving against the real load
 * addresses is what turned that into SwitchToFiber+0x44. */
static USHORT (WINAPI *pRtlCaptureStackBackTrace)(ULONG, ULONG, PVOID *, ULONG *);

static void where(void *addr, char *out, size_t n)
{
    HMODULE mod = NULL;
    char name[MAX_PATH];
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, addr, &mod) && mod &&
        GetModuleFileNameA(mod, name, sizeof(name))) {
        const char *base = strrchr(name, '\\');
        snprintf(out, n, "%s+0x%llx", base ? base + 1 : name,
                 (unsigned long long)((char *)addr - (char *)mod));
        return;
    }
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(addr, &mbi, sizeof(mbi)))
        snprintf(out, n, "<unmapped> state=0x%lx protect=0x%lx", mbi.State, mbi.Protect);
    else
        snprintf(out, n, "<no mapping>");
}

static LONG CALLBACK veh(EXCEPTION_POINTERS *ep)
{
    EXCEPTION_RECORD *er = ep->ExceptionRecord;
    if (er->ExceptionCode != EXCEPTION_ACCESS_VIOLATION &&
        er->ExceptionCode != EXCEPTION_ILLEGAL_INSTRUCTION &&
        er->ExceptionCode != EXCEPTION_PRIV_INSTRUCTION &&
        er->ExceptionCode != EXCEPTION_IN_PAGE_ERROR)
        return EXCEPTION_CONTINUE_SEARCH;
    if (!logf_) return EXCEPTION_CONTINUE_SEARCH;

    char buf[MAX_PATH + 64];
    where(er->ExceptionAddress, buf, sizeof(buf));
    fprintf(logf_, "\n=== exception 0x%08lx at %p  %s\n",
            er->ExceptionCode, er->ExceptionAddress, buf);
    if (er->ExceptionCode == EXCEPTION_ACCESS_VIOLATION && er->NumberParameters >= 2) {
        static const char *op[] = { "read", "write", [8] = "execute" };
        ULONG_PTR kind = er->ExceptionInformation[0];
        void *target = (void *)er->ExceptionInformation[1];
        where(target, buf, sizeof(buf));
        fprintf(logf_, "    %s of %p  %s\n",
                kind <= 8 && op[kind] ? op[kind] : "?", target, buf);
    }
    if (pRtlCaptureStackBackTrace) {
        void *frames[24];
        USHORT n = pRtlCaptureStackBackTrace(0, 24, frames, NULL);
        for (USHORT i = 0; i < n; i++) {
            where(frames[i], buf, sizeof(buf));
            fprintf(logf_, "    [%2u] %p  %s\n", i, frames[i], buf);
        }
    }
    fflush(logf_);
    return EXCEPTION_CONTINUE_SEARCH;
}

/* ----------------------------------------------------------------- fibers */

static void *(WINAPI *real_CreateFiberEx)(SIZE_T, SIZE_T, DWORD, LPFIBER_START_ROUTINE, LPVOID);
static void *(WINAPI *real_ConvertThreadToFiber)(LPVOID);
static void (WINAPI *real_SwitchToFiber)(LPVOID);
static void (WINAPI *real_DeleteFiber)(LPVOID);

static LONG fibers_made, fibers_deleted, switches, bad_switches;

/* what GetCurrentFiber() compiles to, kept for reporting the broken value */
static void *gs_fiber(void)
{
    void *f;
    __asm__ volatile ("movq %%gs:0x20, %0" : "=r" (f));
    return f;
}

/* A job system switches thousands of times a frame, so this stays O(1):
 * anything below 64K or misaligned cannot be a heap pointer. */
static void WINAPI hook_SwitchToFiber(LPVOID fiber)
{
    InterlockedIncrement(&switches);
    if (((ULONG_PTR)fiber < 0x10000 || ((ULONG_PTR)fiber & 7)) &&
        InterlockedIncrement(&bad_switches) <= 4 && logf_) {
        char buf[MAX_PATH + 64];
        void *ret = __builtin_return_address(0);
        where(ret, buf, sizeof(buf));
        fprintf(logf_, "\n=== SwitchToFiber(%p) is not a fiber pointer\n", fiber);
        fprintf(logf_, "    called from %p  %s\n", ret, buf);
        fprintf(logf_, "    thread %lu, switch #%ld, %ld created / %ld deleted\n",
                GetCurrentThreadId(), switches, fibers_made, fibers_deleted);
        fprintf(logf_, "    gs:0x20 reads %p\n", gs_fiber());
        fflush(logf_);
    }
    real_SwitchToFiber(fiber);
}

static void *WINAPI hook_CreateFiberEx(SIZE_T commit, SIZE_T reserve, DWORD flags,
                                       LPFIBER_START_ROUTINE start, LPVOID param)
{
    void *f = real_CreateFiberEx(commit, reserve, flags, start, param);
    LONG n = InterlockedIncrement(&fibers_made);
    if (logf_ && (n <= 8 || !f)) {
        fprintf(logf_, "CreateFiberEx(commit=%llu reserve=%llu) = %p%s\n",
                (unsigned long long)commit, (unsigned long long)reserve, f,
                f ? "" : "  <== FAILED");
        fflush(logf_);
    }
    return f;
}

static void *WINAPI hook_ConvertThreadToFiber(LPVOID param)
{
    void *f = real_ConvertThreadToFiber(param);
    if (logf_) {
        fprintf(logf_, "ConvertThreadToFiber() = %p  thread %lu%s\n",
                f, GetCurrentThreadId(), f ? "" : "  <== FAILED");
        fflush(logf_);
    }
    return f;
}

static void WINAPI hook_DeleteFiber(LPVOID fiber)
{
    if (logf_ && InterlockedIncrement(&fibers_deleted) <= 8) {
        fprintf(logf_, "DeleteFiber(%p) thread %lu\n", fiber, GetCurrentThreadId());
        fflush(logf_);
    }
    real_DeleteFiber(fiber);
}

/* Swap one IAT slot. The imports are by name, so match the hint/name table and
 * patch the parallel address table entry the call actually goes through. */
static int patch_import(HMODULE mod, const char *want, void *repl, void **orig)
{
    char *base = (char *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY *dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir->VirtualAddress) return 0;

    for (IMAGE_IMPORT_DESCRIPTOR *imp = (void *)(base + dir->VirtualAddress); imp->Name; imp++) {
        IMAGE_THUNK_DATA *names = (void *)(base + (imp->OriginalFirstThunk
                                                   ? imp->OriginalFirstThunk : imp->FirstThunk));
        IMAGE_THUNK_DATA *addrs = (void *)(base + imp->FirstThunk);
        for (; names->u1.AddressOfData; names++, addrs++) {
            if (names->u1.Ordinal & IMAGE_ORDINAL_FLAG) continue;
            IMAGE_IMPORT_BY_NAME *n = (void *)(base + names->u1.AddressOfData);
            if (strcmp((const char *)n->Name, want)) continue;

            DWORD old;
            if (!VirtualProtect(addrs, sizeof(*addrs), PAGE_READWRITE, &old)) return 0;
            *orig = (void *)(ULONG_PTR)addrs->u1.Function;
            addrs->u1.Function = (ULONG_PTR)repl;
            VirtualProtect(addrs, sizeof(*addrs), old, &old);
            return 1;
        }
    }
    return 0;
}

/* --------------------------------------------------- the GetCurrentFiber fix
 *
 *     #define GetCurrentFiber()  ((PVOID)__readgsqword(0x20))
 *
 * compiles to `mov %gs:0x20,<reg>` and never enters wine. Measured with
 * fibprobe: gs:0x20 is 0x8ff on every fresh thread and 0x20ff on the main one
 * (a _pthread_priority_t, qos class in the high byte), while TEB+0x20 is 0
 * before a conversion and the real fiber after. Wine cannot fix this -- it
 * cannot move the TEB under gs, because libpthread reserves slots 0-5 which is
 * exactly the range NtTib needs, and it cannot intercept a load. So rewrite the
 * load: each 9-byte site becomes a call to a thunk returning
 * NtCurrentTeb()->Tib.FiberData in the same register.
 *
 * Deliberately narrow: only gs:0x20. Other gs offsets are wrong for the same
 * reason but are not this bug, and guessing at intent is worse than a fault. */
#define GS_READ_LEN 9

static BYTE *thunk_arena;
static SIZE_T thunk_used;
static LONG sites_patched;

static BYTE *alloc_thunk(HMODULE mod, SIZE_T need)
{
    if (!thunk_arena) {
        /* must land within a rel32 of the call sites, so start at the image and
         * walk forward; VirtualAlloc rounds a hint up to the next free region */
        for (SIZE_T off = 0; off < 0x40000000 && !thunk_arena; off += 0x100000)
            thunk_arena = VirtualAlloc((BYTE *)mod + off, 0x1000,
                                       MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
        if (!thunk_arena) return NULL;
    }
    if (thunk_used + need > 0x1000) return NULL;
    BYTE *p = thunk_arena + thunk_used;
    thunk_used += need;
    return p;
}

/* returns TEB->Tib.FiberData in `reg`, preserving every other register */
static BYTE *make_thunk(HMODULE mod, int reg)
{
    BYTE code[32];
    int n = 0, ext = reg >= 8, low = reg & 7;

    if (reg != 0) code[n++] = 0x50;                        /* push %rax */
    code[n++] = 0x65; code[n++] = 0x48; code[n++] = 0x8b;  /* mov %gs:0x30,%rax */
    code[n++] = 0x04; code[n++] = 0x25;
    code[n++] = 0x30; code[n++] = 0x00; code[n++] = 0x00; code[n++] = 0x00;
    code[n++] = ext ? 0x4c : 0x48;                         /* mov 0x20(%rax),<reg> */
    code[n++] = 0x8b;
    code[n++] = 0x40 | (low << 3);
    code[n++] = 0x20;
    if (reg != 0) code[n++] = 0x58;                        /* pop %rax */
    code[n++] = 0xc3;                                      /* ret */

    BYTE *t = alloc_thunk(mod, n);
    if (t) memcpy(t, code, n);
    return t;
}

static void patch_gs_reads(HMODULE mod)
{
    BYTE *base = (BYTE *)mod;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    IMAGE_SECTION_HEADER *sec = IMAGE_FIRST_SECTION(nt);
    BYTE *thunks[16] = { 0 };

    for (WORD s = 0; s < nt->FileHeader.NumberOfSections; s++) {
        if (!(sec[s].Characteristics & IMAGE_SCN_MEM_EXECUTE)) continue;
        BYTE *p = base + sec[s].VirtualAddress;
        BYTE *end = p + sec[s].Misc.VirtualSize - GS_READ_LEN;

        for (; p < end; p++) {
            /* 65 <48|4c> 8b <modrm 04|reg<<3> 25 20 00 00 00 */
            if (p[0] != 0x65 || p[2] != 0x8b || p[4] != 0x25) continue;
            if (p[1] != 0x48 && p[1] != 0x4c) continue;
            if (p[5] != 0x20 || p[6] || p[7] || p[8]) continue;
            if ((p[3] & 0xc7) != 0x04) continue;

            int reg = ((p[3] >> 3) & 7) | (p[1] == 0x4c ? 8 : 0);
            if (!thunks[reg] && !(thunks[reg] = make_thunk(mod, reg))) continue;

            LONGLONG rel = thunks[reg] - (p + 5);
            if (rel < INT32_MIN || rel > INT32_MAX) continue;

            DWORD old;
            if (!VirtualProtect(p, GS_READ_LEN, PAGE_EXECUTE_READWRITE, &old)) continue;
            p[0] = 0xe8;                                   /* call rel32 */
            *(INT32 *)(p + 1) = (INT32)rel;
            memset(p + 5, 0x90, 4);                        /* nop out the tail */
            VirtualProtect(p, GS_READ_LEN, old, &old);
            FlushInstructionCache(GetCurrentProcess(), p, GS_READ_LEN);

            InterlockedIncrement(&sites_patched);
            if (logf_) fprintf(logf_, "  patched GetCurrentFiber() at %p (reg %d)\n", p, reg);
            p += GS_READ_LEN - 1;
        }
    }
    if (logf_) {
        fprintf(logf_, "vkshim: rewrote %ld GetCurrentFiber() site(s) in the exe\n", sites_patched);
        fflush(logf_);
    }
}

static void hook_fibers(void)
{
    HMODULE exe = GetModuleHandleA(NULL);
    int n = 0;
    n += patch_import(exe, "SwitchToFiber", hook_SwitchToFiber, (void **)&real_SwitchToFiber);
    n += patch_import(exe, "CreateFiberEx", hook_CreateFiberEx, (void **)&real_CreateFiberEx);
    n += patch_import(exe, "ConvertThreadToFiber", hook_ConvertThreadToFiber,
                      (void **)&real_ConvertThreadToFiber);
    n += patch_import(exe, "DeleteFiber", hook_DeleteFiber, (void **)&real_DeleteFiber);
    if (logf_) { fprintf(logf_, "vkshim: hooked %d/4 fiber imports\n", n); fflush(logf_); }
    patch_gs_reads(exe);
}

/* ------------------------------------------------- what the masking cost us */

/* A decoration only proves the shader *declares* a builtin. Shader compilers
 * emit whole output blocks whether the stage uses them or not, so "453 shaders
 * mention CullDistance" and "453 shaders depend on culling" are very different
 * claims. Follow the id instead: collect what is decorated CullDistance,
 * propagate through OpAccessChain, and see whether anything is stored into it.
 *
 * Same idea on the pipeline side. depthBoundsTestEnable with the full 0..1
 * range rejects nothing, so the range is what decides whether masking the
 * feature away actually changed a pixel. */
static LONG shaders_seen, shaders_cull, shaders_clip, cull_by_member, cull_written;
static LONG pipes_seen, pipes_depthbounds, pipes_dynbounds, pipes_failed;
static LONG bounds_full_range, bounds_narrowed, n_ranges;
static float seen_min[8], seen_max[8];

#define SPIRV_MAGIC        0x07230203
#define OP_STORE           62
#define OP_ACCESS_CHAIN    65
#define OP_DECORATE        71
#define OP_MEMBER_DECORATE 72
#define DECORATION_BUILTIN 11
#define BUILTIN_CLIP        3
#define BUILTIN_CULL        4

static void scan_spirv(const uint32_t *w, size_t words)
{
    if (words < 5 || w[0] != SPIRV_MAGIC) return;
    uint32_t cull_ids[32];
    unsigned n_cull = 0;
    int cull = 0, clip = 0, via_member = 0, written = 0;

    for (size_t i = 5; i < words; ) {
        uint32_t len = w[i] >> 16, op = w[i] & 0xffff;
        if (len < 1 || i + len > words) break;

        if (op == OP_DECORATE && len >= 4 && w[i + 2] == DECORATION_BUILTIN) {
            if (w[i + 3] == BUILTIN_CULL) {
                cull = 1;
                if (n_cull < 32) cull_ids[n_cull++] = w[i + 1];
            }
            if (w[i + 3] == BUILTIN_CLIP) clip = 1;
        } else if (op == OP_MEMBER_DECORATE && len >= 5 && w[i + 3] == DECORATION_BUILTIN) {
            /* inside a gl_PerVertex-style block: stores go through the block
             * variable, so the id chase below cannot see them */
            if (w[i + 4] == BUILTIN_CULL) { cull = 1; via_member = 1; }
            if (w[i + 4] == BUILTIN_CLIP) clip = 1;
        } else if (op == OP_ACCESS_CHAIN && len >= 4) {
            for (unsigned k = 0; k < n_cull; k++)
                if (cull_ids[k] == w[i + 3]) {
                    if (n_cull < 32) cull_ids[n_cull++] = w[i + 2];
                    break;
                }
        } else if (op == OP_STORE && len >= 3) {
            for (unsigned k = 0; k < n_cull; k++)
                if (cull_ids[k] == w[i + 1]) { written = 1; break; }
        }
        i += len;
    }
    if (cull) InterlockedIncrement(&shaders_cull);
    if (clip) InterlockedIncrement(&shaders_clip);
    if (via_member) InterlockedIncrement(&cull_by_member);
    if (written) InterlockedIncrement(&cull_written);
}

__declspec(dllexport)
VkResult vkCreateShaderModule(VkDevice dev, const VkShaderModuleCreateInfo *ci,
                              const void *alloc, void *out)
{
    if (ci && ci->pCode && ci->codeSize >= 20) {
        InterlockedIncrement(&shaders_seen);
        scan_spirv(ci->pCode, ci->codeSize / 4);
    }
    return real_vkCreateShaderModule(dev, ci, alloc, out);
}

/* offsets into VkGraphicsPipelineCreateInfo and its substructs, rather than
 * restating three large structs we never otherwise touch */
#define GPCI_STRIDE              144
#define GPCI_DEPTH_STENCIL_PTR    80
#define GPCI_DYNAMIC_PTR          96
#define DS_DEPTH_BOUNDS_ENABLE    32
#define DS_MIN_DEPTH_BOUNDS       96
#define DS_MAX_DEPTH_BOUNDS      100
#define DYN_COUNT                 20
#define DYN_STATES_PTR            24
#define VK_DYNAMIC_STATE_DEPTH_BOUNDS 4

static void note_range(float lo, float hi)
{
    if (lo <= 0.0f && hi >= 1.0f) { InterlockedIncrement(&bounds_full_range); return; }
    InterlockedIncrement(&bounds_narrowed);
    for (LONG i = 0; i < n_ranges && i < 8; i++)
        if (seen_min[i] == lo && seen_max[i] == hi) return;
    LONG at = InterlockedIncrement(&n_ranges) - 1;
    if (at < 8) { seen_min[at] = lo; seen_max[at] = hi; }
}

__declspec(dllexport)
VkResult vkCreateGraphicsPipelines(VkDevice dev, void *cache, uint32_t count,
                                   const void *infos, const void *alloc, void *out)
{
    for (uint32_t i = 0; infos && i < count; i++) {
        const char *ci = (const char *)infos + (size_t)i * GPCI_STRIDE;
        InterlockedIncrement(&pipes_seen);

        const char *ds = *(const char *const *)(ci + GPCI_DEPTH_STENCIL_PTR);
        if (ds && *(const uint32_t *)(ds + DS_DEPTH_BOUNDS_ENABLE)) {
            InterlockedIncrement(&pipes_depthbounds);
            note_range(*(const float *)(ds + DS_MIN_DEPTH_BOUNDS),
                       *(const float *)(ds + DS_MAX_DEPTH_BOUNDS));
        }

        const char *dyn = *(const char *const *)(ci + GPCI_DYNAMIC_PTR);
        if (dyn) {
            uint32_t n = *(const uint32_t *)(dyn + DYN_COUNT);
            const uint32_t *st = *(const uint32_t *const *)(dyn + DYN_STATES_PTR);
            for (uint32_t j = 0; st && j < n; j++)
                if (st[j] == VK_DYNAMIC_STATE_DEPTH_BOUNDS) {
                    InterlockedIncrement(&pipes_dynbounds);
                    break;
                }
        }
    }

    VkResult r = real_vkCreateGraphicsPipelines(dev, cache, count, infos, alloc, out);
    if (r) InterlockedIncrement(&pipes_failed);

    /* one block every 256 pipelines: a level builds thousands and the answer is
     * a running total, not a per-call trace */
    if (logf_ && pipes_seen && !(pipes_seen & 0xff)) {
        fprintf(logf_, "tally: %ld shaders, %ld decorate CullDistance "
                       "(%ld only inside a block, %ld actually store to it), %ld ClipDistance\n",
                shaders_seen, shaders_cull, cull_by_member, cull_written, shaders_clip);
        fprintf(logf_, "       %ld pipelines, %ld enable depthBounds "
                       "(%ld full 0..1 = no-op, %ld narrowed), %ld dynamic, %ld failed\n",
                pipes_seen, pipes_depthbounds, bounds_full_range, bounds_narrowed,
                pipes_dynbounds, pipes_failed);
        for (LONG i = 0; i < n_ranges && i < 8; i++)
            fprintf(logf_, "       narrowed range in use: %.4f .. %.4f\n", seen_min[i], seen_max[i]);
        fflush(logf_);
    }
    return r;
}

/* ------------------------------------------------------------------- setup */

static void shim_init(void)
{
    static int done;
    if (done) return;
    done = 1;

    char path[MAX_PATH];
    if (!GetEnvironmentVariableA("VKSHIM_LOG", path, sizeof(path)))
        strcpy(path, "vkshim.log");
    logf_ = fopen(path, "a");

    char m[8];
    mask_mode = !(GetEnvironmentVariableA("VKSHIM_MASK", m, sizeof(m)) && m[0] == '0');

    HMODULE nt = GetModuleHandleA("ntdll.dll");
    if (nt) pRtlCaptureStackBackTrace = (void *)GetProcAddress(nt, "RtlCaptureStackBackTrace");
    AddVectoredExceptionHandler(1, veh);
    hook_fibers();

    HMODULE wv = LoadLibraryA("winevulkan.dll");
    if (!wv) { if (logf_) { fprintf(logf_, "vkshim: no winevulkan.dll\n"); fflush(logf_); } return; }
    real_vkCreateDevice = (void *)GetProcAddress(wv, "vkCreateDevice");
    real_vkGetInstanceProcAddr = (void *)GetProcAddress(wv, "vkGetInstanceProcAddr");
    real_vkGetDeviceProcAddr = (void *)GetProcAddress(wv, "vkGetDeviceProcAddr");
    real_vkGetPhysicalDeviceFeatures = (void *)GetProcAddress(wv, "vkGetPhysicalDeviceFeatures");
    real_vkCreateShaderModule = (void *)GetProcAddress(wv, "vkCreateShaderModule");
    real_vkCreateGraphicsPipelines = (void *)GetProcAddress(wv, "vkCreateGraphicsPipelines");
}

/* Report every bit the app asked for against what the driver advertises, then
 * either mask the offenders or leave them so the call fails as it would have. */
static void triage(VkPhysicalDevice phys, VkPhysicalDeviceFeatures *want, const char *stage)
{
    VkPhysicalDeviceFeatures have = { 0 };
    if (real_vkGetPhysicalDeviceFeatures) real_vkGetPhysicalDeviceFeatures(phys, &have);

    uint32_t *w = (uint32_t *)want;
    const uint32_t *h = (const uint32_t *)&have;
    unsigned bad = 0;

    for (unsigned i = 0; i < FEATURE_COUNT; i++) {
        if (!w[i]) continue;
        if (h[i]) {
            if (logf_) fprintf(logf_, "  ok      %s\n", FEATURE_NAMES[i]);
        } else {
            bad++;
            if (logf_) fprintf(logf_, "  MISSING %s%s\n", FEATURE_NAMES[i],
                               mask_mode ? "  (masked off)" : "  <== this is why the call fails");
            if (mask_mode) w[i] = 0;
        }
    }
    if (logf_) {
        fprintf(logf_, "vkshim: %s enabled features, %u unsupported%s\n",
                stage, bad, bad && !mask_mode ? " -> VK_ERROR_FEATURE_NOT_PRESENT" : "");
        fflush(logf_);
    }
}

__declspec(dllexport)
VkResult vkCreateDevice(VkPhysicalDevice phys, const VkDeviceCreateInfo *ci,
                        const void *alloc, VkDevice *dev)
{
    shim_init();
    if (!real_vkCreateDevice) return -3;                /* VK_ERROR_INITIALIZATION_FAILED */
    if (!ci) return real_vkCreateDevice(phys, ci, alloc, dev);

    if (logf_) {
        fprintf(logf_, "\n=== vkCreateDevice: %u extensions\n", ci->enabledExtensionCount);
        for (uint32_t i = 0; i < ci->enabledExtensionCount; i++)
            fprintf(logf_, "  ext %s\n", ci->ppEnabledExtensionNames[i]);
    }

    VkDeviceCreateInfo copy = *ci;
    VkPhysicalDeviceFeatures feats;
    VkPhysicalDeviceFeatures2 f2copy;

    if (ci->pEnabledFeatures) {
        feats = *ci->pEnabledFeatures;
        triage(phys, &feats, "pEnabledFeatures");
        copy.pEnabledFeatures = &feats;
    }

    /* A 1.1-era title puts them in a VkPhysicalDeviceFeatures2 on pNext instead.
     * Only the head link is rewritten; deeper structs are feature-specific and
     * do not carry the 55 core bools. */
    for (const uint32_t *p = ci->pNext; p; p = *(const void *const *)((const char *)p + 8)) {
        if (*p != VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2) continue;
        f2copy = *(const VkPhysicalDeviceFeatures2 *)p;
        triage(phys, &f2copy.features, "VkPhysicalDeviceFeatures2");
        if (p == ci->pNext) copy.pNext = &f2copy;
        else if (logf_) fprintf(logf_, "vkshim: features2 not at head of pNext, not rewritten\n");
        break;
    }

    VkResult r = real_vkCreateDevice(phys, &copy, alloc, dev);
    if (logf_) { fprintf(logf_, "vkshim: vkCreateDevice -> %d\n", r); fflush(logf_); }
    return r;
}

__declspec(dllexport)
PFN_vkVoidFunction vkGetDeviceProcAddr(VkDevice dev, const char *name)
{
    shim_init();
    if (!name || !real_vkGetDeviceProcAddr) return NULL;
    if (!strcmp(name, "vkCreateShaderModule")) return (PFN_vkVoidFunction)vkCreateShaderModule;
    if (!strcmp(name, "vkCreateGraphicsPipelines"))
        return (PFN_vkVoidFunction)vkCreateGraphicsPipelines;
    return real_vkGetDeviceProcAddr(dev, name);
}

__declspec(dllexport)
PFN_vkVoidFunction vkGetInstanceProcAddr(VkInstance inst, const char *name)
{
    shim_init();
    if (!name) return real_vkGetInstanceProcAddr ? real_vkGetInstanceProcAddr(inst, name) : NULL;
    if (!strcmp(name, "vkCreateDevice")) return (PFN_vkVoidFunction)vkCreateDevice;
    if (!strcmp(name, "vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)vkGetInstanceProcAddr;
    if (!strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)vkGetDeviceProcAddr;
    if (!strcmp(name, "vkCreateShaderModule")) return (PFN_vkVoidFunction)vkCreateShaderModule;
    if (!strcmp(name, "vkCreateGraphicsPipelines"))
        return (PFN_vkVoidFunction)vkCreateGraphicsPipelines;
    return real_vkGetInstanceProcAddr ? real_vkGetInstanceProcAddr(inst, name) : NULL;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved)
{
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(h);
    return TRUE;
}
