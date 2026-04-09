#include <stdio.h>
#include <inttypes.h>

#include "nyet_runtime.h"

void nyet_println(const char* cstr) {
    if (cstr != NULL) {
        fputs(cstr, stdout);
    }
    fputc('\n', stdout);
}

void nyet_print_i32(int32_t value) {
    fprintf(stdout, "%" PRId32, value);
}

void nyet_print_string(nyet_string s) {
    if (s.data != NULL && s.len > 0) {
        fwrite(s.data, 1, s.len, stdout);
    }
}
