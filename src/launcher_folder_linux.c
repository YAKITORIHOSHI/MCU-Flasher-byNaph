/* Native first-click entries inside LAUNCHER. Rebuild with
 * direct/build_launcher_folder.py on the matching Linux CPU. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int unavailable(const char *platform) {
    char message[512];
    int count = snprintf(message, sizeof message,
        "MCU Flasher for %s is not available in this release.\n"
        "The native application and firmware tools are still being implemented.\n"
        "This notice does not install or start the Ubuntu application.", platform);
    if (count < 0 || (size_t)count >= sizeof message) return 1;
    fprintf(stderr, "%s\n", message);
    /* The binary has no terminal when double-clicked. Use an available native
     * dialog without a shell, package installation or application startup. */
    const char *dialogs[] = {"/usr/bin/zenity", "/usr/bin/kdialog", "/usr/bin/xmessage"};
    for (size_t index = 0; index < sizeof dialogs / sizeof *dialogs; ++index) {
        if (access(dialogs[index], X_OK) != 0) continue;
        pid_t child = fork();
        if (child == 0) {
            if (index == 0)
                execl(dialogs[index], dialogs[index], "--info", "--title=MCU Flasher",
                      "--no-markup", "--text", message, (char *)NULL);
            else if (index == 1)
                execl(dialogs[index], dialogs[index], "--title", "MCU Flasher",
                      "--msgbox", message, (char *)NULL);
            else
                execl(dialogs[index], dialogs[index], "-center", "-title", "MCU Flasher",
                      "--", message, (char *)NULL);
            _exit(127);
        }
        if (child > 0) {
            int status = 0;
            pid_t waited;
            do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
            if (waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0) break;
        }
    }
    return 1;
}

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
    if (strcmp(name, "FEDORA") == 0 || strcmp(name, "FEDORA_ARM64") == 0)
        return unavailable("Fedora");
    if (strcmp(name, "LINUX_MINT") == 0) return unavailable("Linux Mint");
    if (strcmp(name, "UBUNTU") != 0 && strcmp(name, "UBUNTU_ARM64") != 0) {
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
    perror("Cannot start the Ubuntu launcher");
    free(script); free(command);
    return error == ENOENT ? 127 : 1;
}
