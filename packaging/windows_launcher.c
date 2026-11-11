#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

enum { PATH_CAPACITY = 32768 };
static wchar_t root[PATH_CAPACITY], python[PATH_CAPACITY], script[PATH_CAPACITY];
static wchar_t command[PATH_CAPACITY];

static int launch_error(const wchar_t *message) {
    MessageBoxW(NULL, message, L"LinkRadar", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance;
    (void)previous;
    (void)show;

    DWORD length = GetModuleFileNameW(NULL, root, PATH_CAPACITY);
    if (!length || length == PATH_CAPACITY) {
        return launch_error(L"Unable to locate LinkRadar.");
    }
    wchar_t *separator = wcsrchr(root, L'\\');
    if (!separator) {
        return launch_error(L"Unable to locate the installation directory.");
    }
    *separator = L'\0';
    if (swprintf_s(python, PATH_CAPACITY, L"%ls\\runtime\\pythonw.exe", root) < 0 ||
        swprintf_s(script, PATH_CAPACITY, L"%ls\\app\\desktop.py", root) < 0 ||
        GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        return launch_error(L"The LinkRadar installation is incomplete. Run LinkRadar Setup again.");
    }
    if (swprintf_s(command, PATH_CAPACITY, L"\"%ls\" -B \"%ls\" %ls", python, script, arguments) < 0) {
        return launch_error(L"Unable to start LinkRadar: command line too long.");
    }

    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                        NULL, root, &startup, &process)) {
        return launch_error(L"Unable to start LinkRadar.");
    }
    CloseHandle(process.hThread);
    DWORD exit_code = 1;
    if (WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0) {
        GetExitCodeProcess(process.hProcess, &exit_code);
    }
    CloseHandle(process.hProcess);
    return (int)exit_code;
}
