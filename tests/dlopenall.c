/* every bundled dylib must actually load.
 *
 * the other media checks run on the builder, where the /nix/store originals
 * still satisfy what the shipped copies cannot, so they pass on broken
 * tarballs. loading the bundled files by path is the only honest test.
 *
 * build x86_64: the bundle is thinned to x86_64 and arm64 cannot load it.
 *
 * build: clang -arch x86_64 -O2 -o dlopenall dlopenall.c
 * usage: find <libdir> -name '*.dylib*' -o -name '*.so' | xargs ./dlopenall
 */
#include <dlfcn.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    int failed = 0;

    for (int i = 1; i < argc; i++) {
        /* NOW, not LAZY: a lazy load hides a missing function until it is
         * called, and on a good bundle both modes agree at 0 failures */
        if (!dlopen(argv[i], RTLD_NOW)) {
            printf("FAIL %s\n", dlerror());
            failed++;
        }
    }

    printf("RESULT: %d of %d bundled libraries failed to load\n", failed, argc - 1);
    return failed ? 1 : 0;
}
