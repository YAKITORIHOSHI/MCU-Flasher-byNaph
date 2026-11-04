/* Native first-click entries inside LAUNCHER. Rebuild with
 * direct/build_launcher_folder.py on the matching Linux CPU. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char path[65536];
    ssize_t length = readlink("/proc/self/exe", path, sizeof path - 1);
    if (length < 0 || (size_t)length >= sizeof path - 1) {
        fprintf(stderr, "Cannot locate the MCU Flasher executable.\n");
        return 1;
    }
    path[length] = '\0';
    char *separator = strrchr(path, '/');
    if (!separator) return 1;
    const char *name = separator + 1;
    /* run.sh performs admission on OS release data, never on this file's name. */
    if (strcmp(name, "UBUNTU") != 0 && strcmp(name, "UBUNTU_ARM64") != 0
            && strcmp(name, "FEDORA") != 0 && strcmp(name, "FEDORA_ARM64") != 0
            && strcmp(name, "LINUX_MINT") != 0) {
        fprintf(stderr, "Use the named executable supplied in LAUNCHER.\n");
        return 1;
    }
    *separator = '\0';
    separator = strrchr(path, '/');
    if (!separator || strcmp(separator + 1, "LAUNCHER") != 0) {
        fprintf(stderr, "Keep the executable inside the application's LAUNCHER folder.\n");
        return 1;
    }
    *separator = '\0';
    const char suffix[] = "/direct/ubuntu/run.sh";
    char *script = malloc(strlen(path) + sizeof suffix);
    char **command = calloc((size_t)argc + 3, sizeof *command);
    if (!script || !command) {
        free(script); free(command);
        return 1;
    }
    strcpy(script, path);
    strcat(script, suffix);
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "Cannot locate direct/ubuntu/run.sh. Keep the complete application folder together.\n");
        free(script); free(command);
        return 1;
    }
    char shell[] = "/bin/bash";
    char desktop_option[] = "--desktop";
    command[0] = shell;
    command[1] = script;
    command[2] = desktop_option;
    for (int index = 1; index < argc; ++index) command[index + 2] = argv[index];
    execv(command[0], command);
    int error = errno;
    perror("Cannot start the MCU Flasher launcher");
    free(script); free(command);
    return error == ENOENT ? 127 : 1;
}
