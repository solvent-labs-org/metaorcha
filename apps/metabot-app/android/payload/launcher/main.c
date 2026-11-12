/* E1: python3.14 for Android, shipped in lib/arm64-v8a/ so the app may exec it.
   The pinned binary (packaging/android-wheels.lock) was built from this file with the
   partial NDK r27d pull; rebuilding wants that pull approved (see the lock header). */
   The same binary is installed under three names and dispatches on its own basename:
     libmetabot_py.so   -> python <args>
     libmetabot_cli.so  -> python -m metabot <args>      (the app's metabot_bin)
     liborcha_cli.so    -> python -m emerge.cli <args>   (the app's orcha_bin)
   No system headers on purpose: linked against the NDK's libc.so stub only. */
int Py_BytesMain(int argc, char **argv);
int sigprocmask(int how, const unsigned long *set, unsigned long *old);
char *strrchr(const char *s, int c);
int strcmp(const char *a, const char *b);

static int endswith(const char *s, const char *tail) {
    const char *base = strrchr(s, '/');
    base = base ? base + 1 : s;
    return strcmp(base, tail) == 0;
}

int main(int argc, char **argv) {
    /* An app process blocks SIGUSR1 and the mask survives exec; give Python a clean one.
       SIG_UNBLOCK == 1 on bionic LP64. */
    unsigned long all = ~0UL;
    sigprocmask(1, &all, 0);
    const char *module = 0;
    if (argc > 0 && endswith(argv[0], "libmetabot_cli.so")) module = "metabot";
    else if (argc > 0 && endswith(argv[0], "liborcha_cli.so")) module = "emerge.cli";
    if (!module) return Py_BytesMain(argc, argv);
    char *nargv[argc + 3];
    nargv[0] = argv[0];
    nargv[1] = "-m";
    nargv[2] = (char *)module;
    for (int i = 1; i <= argc; i++) nargv[i + 2] = argv[i];   /* copies the final NULL too */
    return Py_BytesMain(argc + 2, nargv);
}
