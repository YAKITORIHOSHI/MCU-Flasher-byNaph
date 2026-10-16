/* Native Ubuntu launcher: MCU_Flasher (amd64), MCU_Flasher-arm64 (ARM64).
 * Keep the matching binary beside direct/, main/ and src/. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    size_t capacity = 256;
    char *path = NULL;
    for (;;) {
        char *grown = realloc(path, capacity);
        if (!grown) {
            free(path);
            perror("MCU Flasher launcher");
            return 1;
        }
        path = grown;
        ssize_t length = readlink("/proc/self/exe", path, capacity - 1);
        if (length < 0) {
            perror("Cannot locate the MCU Flasher application folder");
            free(path);
            return 1;
        }
        if ((size_t)length < capacity - 1) {
            path[length] = '\0';
            break;
        }
        capacity *= 2;
    }
    char *separator = strrchr(path, '/');
    if (!separator) {
        free(path);
        return 1;
    }
    *separator = '\0';
    const char suffix[] = "/direct/ubuntu/run.sh";
    char *script = malloc(strlen(path) + sizeof suffix);
    char **command = calloc((size_t)argc + 3, sizeof *command);
    if (!script || !command) {
        perror("MCU Flasher launcher");
        free(command);
        free(script);
        free(path);
        return 1;
    }
    strcpy(script, path);
    strcat(script, suffix);
    free(path);
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "Keep MCU_Flasher beside its direct/, main/ and src/ folders.\n");
        perror(script);
        free(command);
        free(script);
        return 1;
    }
    char shell[] = "/bin/bash";
    char desktop_option[] = "--desktop";
    command[0] = shell;
    command[1] = script;
    command[2] = desktop_option;
    for (int index = 1; index < argc; ++index)
        command[index + 2] = argv[index];
    /* exec forwards every argument verbatim; no shell command interpolation. */
    execv(command[0], command);
    const int error = errno;
    perror("Cannot start the Ubuntu launcher");
    free(command);
    free(script);
    return error == ENOENT ? 127 : 1;
}
