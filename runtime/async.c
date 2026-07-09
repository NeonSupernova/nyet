/* spawn/await runtime: real OS-thread concurrency, not stackless
 * coroutines. See CONTINUATION_PLAN.md's typed-IR-layer decision for
 * why -- a true async state-machine transform wants a typed IR this
 * compiler doesn't have, so `spawn` runs the call on a pthread and
 * `await` blocks on a join instead.
 *
 * Scoped to functions whose single parameter and return value are
 * both `ptr` (string/struct/sum-type/array/tuple in Nyet's codegen --
 * i.e. everything except bare scalars) -- that shape already matches
 * pthread's own `void *(*)(void *)` start-routine signature exactly,
 * so the target function runs directly as the thread body with no
 * trampoline needed, and its return value comes back through
 * pthread_join's own retval out-parameter.
 */

#include <pthread.h>
#include <stdlib.h>

typedef struct {
    pthread_t thread;
} nyet_handle;

nyet_handle *nyet_spawn(void *(*fn)(void *), void *arg) {
    nyet_handle *h = (nyet_handle *)malloc(sizeof(nyet_handle));
    pthread_create(&h->thread, NULL, fn, arg);
    return h;
}

void *nyet_await(nyet_handle *h) {
    void *result = NULL;
    pthread_join(h->thread, &result);
    free(h);
    return result;
}
