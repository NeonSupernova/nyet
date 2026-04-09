#include <string.h>

#include "nyet_runtime.h"

nyet_string nyet_string_from_cstr(const char* cstr) {
    nyet_string s;
    if (cstr == NULL) {
        s.data = NULL;
        s.len = 0;
        return s;
    }
    size_t len = strlen(cstr);
    char* buf = (char*)nyet_alloc(len);
    if (len != 0) {
        memcpy(buf, cstr, len);
    }
    s.data = buf;
    s.len = len;
    return s;
}

void nyet_string_drop(nyet_string s) {
    nyet_free(s.data);
}
