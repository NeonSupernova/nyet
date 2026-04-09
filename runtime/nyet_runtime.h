#ifndef NYET_RUNTIME_H
#define NYET_RUNTIME_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Allocation */
void* nyet_alloc(size_t bytes);
void  nyet_free(void* ptr);

/* Strings -- Nyet strings are owned UTF-8 slices. */
typedef struct {
    char*  data;
    size_t len;
} nyet_string;

nyet_string nyet_string_from_cstr(const char* cstr);
void        nyet_string_drop(nyet_string s);

/* I/O */
void nyet_println(const char* cstr);
void nyet_print_i32(int32_t value);
void nyet_print_string(nyet_string s);

#ifdef __cplusplus
}
#endif

#endif /* NYET_RUNTIME_H */
