/* Map[string V] / Set[T] runtime: an open-addressing hash table keyed
 * by C strings, with values stored as generic 8-byte slots (the caller
 * casts back to the real Nyet type, which codegen already tracks
 * statically per binding -- same approach as Array[T] element types).
 *
 * Self-contained: does not use nyet_runtime.h's `nyet_string` (a
 * length-prefixed fat pointer) since codegen represents Nyet strings as
 * plain null-terminated C strings, not that type.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char* key; /* owned copy; NULL means empty slot */
    int64_t value;
    int occupied;
} nyet_map_entry;

typedef struct {
    nyet_map_entry* entries;
    int64_t capacity;
    int64_t count;
} nyet_map;

static uint64_t nyet_map_hash(const char* s) {
    uint64_t h = 1469598103934665603ULL; /* FNV-1a offset basis */
    for (const unsigned char* p = (const unsigned char*)s; *p; p++) {
        h ^= *p;
        h *= 1099511628211ULL; /* FNV prime */
    }
    return h;
}

static nyet_map_entry* nyet_map_alloc_entries(int64_t capacity) {
    nyet_map_entry* entries = (nyet_map_entry*)calloc((size_t)capacity, sizeof(nyet_map_entry));
    if (entries == NULL) {
        fputs("nyet_map: out of memory\n", stderr);
        exit(1);
    }
    return entries;
}

nyet_map* nyet_map_new(int64_t initial_capacity) {
    nyet_map* m = (nyet_map*)malloc(sizeof(nyet_map));
    if (m == NULL) {
        fputs("nyet_map: out of memory\n", stderr);
        exit(1);
    }
    m->capacity = initial_capacity > 0 ? initial_capacity : 16;
    m->count = 0;
    m->entries = nyet_map_alloc_entries(m->capacity);
    return m;
}

void nyet_map_set(nyet_map* m, const char* key, int64_t value) {
    if (m->count * 2 >= m->capacity) {
        nyet_map_entry* old_entries = m->entries;
        int64_t old_capacity = m->capacity;
        m->capacity *= 2;
        m->entries = nyet_map_alloc_entries(m->capacity);
        m->count = 0;
        for (int64_t i = 0; i < old_capacity; i++) {
            if (old_entries[i].occupied) {
                nyet_map_set(m, old_entries[i].key, old_entries[i].value);
                free(old_entries[i].key);
            }
        }
        free(old_entries);
    }

    uint64_t h = nyet_map_hash(key);
    int64_t idx = (int64_t)(h % (uint64_t)m->capacity);
    for (int64_t i = 0; i < m->capacity; i++) {
        int64_t slot = (idx + i) % m->capacity;
        if (!m->entries[slot].occupied) {
            m->entries[slot].key = strdup(key);
            m->entries[slot].value = value;
            m->entries[slot].occupied = 1;
            m->count++;
            return;
        }
        if (strcmp(m->entries[slot].key, key) == 0) {
            m->entries[slot].value = value;
            return;
        }
    }
}

int64_t nyet_map_get(nyet_map* m, const char* key, int64_t default_value) {
    uint64_t h = nyet_map_hash(key);
    int64_t idx = (int64_t)(h % (uint64_t)m->capacity);
    for (int64_t i = 0; i < m->capacity; i++) {
        int64_t slot = (idx + i) % m->capacity;
        if (!m->entries[slot].occupied) return default_value;
        if (strcmp(m->entries[slot].key, key) == 0) return m->entries[slot].value;
    }
    return default_value;
}

int nyet_map_contains(nyet_map* m, const char* key) {
    uint64_t h = nyet_map_hash(key);
    int64_t idx = (int64_t)(h % (uint64_t)m->capacity);
    for (int64_t i = 0; i < m->capacity; i++) {
        int64_t slot = (idx + i) % m->capacity;
        if (!m->entries[slot].occupied) return 0;
        if (strcmp(m->entries[slot].key, key) == 0) return 1;
    }
    return 0;
}

int64_t nyet_map_count(nyet_map* m) {
    return m->count;
}

/* Set[T] is a Map[T, _] that never reads the value slot. */

nyet_map* nyet_set_new(int64_t initial_capacity) {
    return nyet_map_new(initial_capacity);
}

void nyet_set_add(nyet_map* m, const char* key) {
    nyet_map_set(m, key, 1);
}

int nyet_set_contains(nyet_map* m, const char* key) {
    return nyet_map_contains(m, key);
}

int64_t nyet_set_count(nyet_map* m) {
    return nyet_map_count(m);
}
