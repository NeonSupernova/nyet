/* Windows conhost only interprets ANSI/VT escape sequences (what
 * std/ansi.no and everything that uses it -- the arcade suite, the
 * demo/features color programs -- print for color) if a program
 * explicitly opts in via SetConsoleMode. Without that, a raw escape
 * code like "\x1b[31m" prints as literal garbage characters instead
 * of red text. Linked into every compiled Nyet binary (see
 * pynyet/driver.py's _cmd_build) so this "just works" with zero
 * source-level changes, no matter what the program prints.
 *
 * A constructor runs this before `main`, since colored output can
 * happen from anywhere, not just after some Nyet-visible setup call.
 * Entirely a no-op outside of Windows -- everything here is guarded
 * out, so this compiles to an empty translation unit on macOS/Linux.
 */
#ifdef _WIN32
#include <windows.h>

static void nyet_enable_vt_processing(HANDLE h) {
    if (h == INVALID_HANDLE_VALUE || h == NULL) return;
    DWORD mode = 0;
    /* GetConsoleMode fails when the handle isn't a real console (e.g.
     * output redirected to a file or piped) -- leave it alone then,
     * there's nothing to enable and nothing broken either way. */
    if (!GetConsoleMode(h, &mode)) return;
    SetConsoleMode(h, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
}

__attribute__((constructor))
static void nyet_win_console_init(void) {
    nyet_enable_vt_processing(GetStdHandle(STD_OUTPUT_HANDLE));
    nyet_enable_vt_processing(GetStdHandle(STD_ERROR_HANDLE));
}
#endif
