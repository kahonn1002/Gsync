/*
 * Gsync.app's executable: runs gsync.py inside this process with Homebrew's Python.
 *
 * Because Python runs in-process (not exec'd), macOS sees the app as Gsync: its name in the Dock,
 * menu bar and Cmd-Tab, its icon, its notifications and its privacy prompts.
 *
 * Python is loaded at run time through Homebrew's version-independent opt/python3 link, so Homebrew
 * upgrading Python (together with pygobject3) doesn't break the app. install_mac.sh compiles this
 * with PYTHON_LIB and GSYNC_SCRIPT defined, and with Homebrew's lib folder as an rpath: GObject
 * introspection opens libraries such as libgobject-2.0.0.dylib by bare name, the same way Homebrew's
 * own python3 binary finds them.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <stdlib.h>
#include <unistd.h>

#if !defined(PYTHON_LIB) || !defined(GSYNC_SCRIPT)
#error "build with install_mac.sh"
#endif

static int fail(const char *detail) {
    CFStringRef title = CFSTR("Gsync can't start");
    CFStringRef text = CFStringCreateWithCString(NULL, detail, kCFStringEncodingUTF8);
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, NULL, NULL, NULL, title, text,
                                   NULL, NULL, NULL, NULL);
    CFRelease(text);
    return 1;
}

int main(int argc, char **argv) {
    if (access(GSYNC_SCRIPT, R_OK) != 0)
        return fail("Its program file " GSYNC_SCRIPT " is missing. If you moved the Gsync folder, "
                    "run install_mac.sh again from its new place.");

    void *python = dlopen(PYTHON_LIB, RTLD_NOW | RTLD_GLOBAL);
    int (*bytes_main)(int, char **) = python ? (int (*)(int, char **))dlsym(python, "Py_BytesMain") : NULL;
    if (!bytes_main)
        return fail("Homebrew's Python was not found at " PYTHON_LIB ". Install it with "
                    "\"brew install pygobject3\", then run install_mac.sh again.");

    /* python <gsync.py> <our arguments...> */
    char **args = calloc((size_t)argc + 2, sizeof(char *));
    if (!args)
        return 1;
    args[0] = argv[0];
    args[1] = GSYNC_SCRIPT;
    for (int i = 1; i < argc; i++)
        args[i + 1] = argv[i];
    return bytes_main(argc + 1, args);
}
