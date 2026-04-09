#include <stdio.h>
#include <stdlib.h>

#include "nyet_runtime.h"

void* nyet_alloc(size_t bytes) {
    void* ptr = malloc(bytes);
    if (ptr == NULL && bytes != 0) {
        fputs("nyet_alloc: out of memory\n", stderr);
        abort();
    }
    return ptr;
}

void nyet_free(void* ptr) {
    free(ptr);
}
