#include <windows.h>
#include <stdio.h>

#ifdef TEST_IMPLEMENTATION
void *WINAPI FlsGetValue2(DWORD);
#endif

int main(void)
{
    const char *modules[] = {"kernelbase.dll", "kernel32.dll"};
    DWORD slot = FlsAlloc(NULL);
    unsigned int i, j, failures = 0;
    int value;

    if (slot == FLS_OUT_OF_INDEXES) return 2;
    for (i = 0; i < 2; ++i)
    {
        void *(WINAPI *get)(DWORD);
#ifdef TEST_IMPLEMENTATION
        get = FlsGetValue2;
#else
        get = (void *)GetProcAddress(GetModuleHandleA(modules[i]), "FlsGetValue2");
#endif
        if (!get) { printf("%s: missing export\n", modules[i]); ++failures; continue; }
        for (j = 0; j < 3; ++j)
        {
            void *expected = j == 0 ? &value : NULL;
            void *actual;
            DWORD error;
            if (!FlsSetValue(slot, expected)) return 2;
            SetLastError(0xdeadbeef);
            actual = get(j == 2 ? FLS_OUT_OF_INDEXES : slot);
            error = GetLastError();
            printf("%s case %u: value=%s last-error=%08lx\n", modules[i], j,
                   actual == expected ? "ok" : "wrong", error);
            if (actual != expected || error != 0xdeadbeef) ++failures;
        }
    }
    FlsFree(slot);
    printf("failures: %u\n", failures);
    return failures ? 1 : 0;
}
