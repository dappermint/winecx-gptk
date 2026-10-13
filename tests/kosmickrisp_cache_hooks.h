#include <stdlib.h>

int kk_test_atexit(void (*callback)(void));
void *kk_test_calloc(size_t count, size_t size);

#define atexit kk_test_atexit
#define calloc kk_test_calloc
